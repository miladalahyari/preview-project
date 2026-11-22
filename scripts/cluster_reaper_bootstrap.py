#!/usr/bin/env python3
"""Run the reaper and its catalog from one protected GitLab main revision."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config/platform.json").read_text())
API = CONFIG["gitlabApiUrl"]
PROJECT = CONFIG["centralProjectId"]
ROOT = Path("/work")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("GitLab redirect refused")


def fetch(path, token, raw=False):
    request = urllib.request.Request(API + path, headers={"PRIVATE-TOKEN": token}, method="GET")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        data = response.read()
    return data if raw else json.loads(data)


def main():
    token = os.environ.get("PREVIEW_GITLAB_TOKEN", "")
    if not token:
        raise RuntimeError("CronJob GitLab token is missing")
    project = fetch(f"/projects/{PROJECT}", token)
    if project.get("path_with_namespace") != CONFIG["centralProjectPath"]:
        raise RuntimeError("Unexpected GitLab project")
    branch = fetch(f"/projects/{PROJECT}/repository/branches/{CONFIG['stateRef']}", token)
    revision = branch.get("commit", {}).get("id", "")
    if branch.get("protected") is not True or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Protected main revision is required")
    entries = fetch(f"/projects/{PROJECT}/repository/tree?path=catalog&ref={revision}&per_page=100", token)
    if not isinstance(entries, list) or len(entries) >= 100:
        raise RuntimeError("Catalog listing is incomplete")
    names = [item["name"] for item in entries if item.get("type") == "blob"
             and re.fullmatch(r"[a-z0-9][a-z0-9-]*\.json", item.get("name", ""))]
    if not names:
        raise RuntimeError("No trusted catalog entries found")
    files = ["scripts/preview.py", "config/platform.json"] + ["catalog/" + name for name in names]
    for relative in files:
        data = fetch(f"/projects/{PROJECT}/repository/files/{urllib.parse.quote(relative, safe='')}/raw?ref={revision}", token, raw=True)
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    env = dict(os.environ, PREVIEW_CODE_REVISION=revision)
    subprocess.run([sys.executable, str(ROOT / "scripts/preview.py"), "reap-kubernetes"],
                   cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, urllib.error.URLError, subprocess.CalledProcessError) as error:
        print("Cluster reaper failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
