#!/usr/bin/env python3
"""Application-side build only: no infrastructure write credentials."""

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config/platform.json").read_text())

TAG_PATTERN = r"test[-.]?[a-z0-9]+([._-][a-z0-9]+)*"
DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlparse(req.full_url), urlparse(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise ValueError("Refusing a credential-bearing redirect to another origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = build_opener(SameOriginRedirects())


def fetch(url, headers=None):
    if urlparse(url).scheme != "https":
        raise ValueError("CI API and registry access require HTTPS")
    with OPENER.open(Request(url, headers=headers or {}), timeout=30) as response:
        return response.read(), response.headers


def validate_identity(env):
    if any(env.get(key) for key in ("HELM_TAG_TOKEN", "PREVIEW_GITLAB_TOKEN", "ARGOCD_AUTH_TOKEN", "KUBECONFIG", "KUBE_TOKEN")):
        raise ValueError("Preview source jobs must not receive Helm/cluster write credentials; scope production variables to production environments")
    for key in ("CI_PROJECT_ID", "CI_PIPELINE_ID"):
        if not re.fullmatch(r"[1-9][0-9]*", env.get(key, "")):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[0-9a-f]{40}", env.get("CI_COMMIT_SHA", "")):
        raise ValueError("Invalid CI_COMMIT_SHA")
    tag = env.get("CI_COMMIT_TAG", "")
    if len(tag) > 128 or not re.fullmatch(TAG_PATTERN, tag):
        raise ValueError("Invalid test tag (maximum 128 characters)")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", env.get("TEST_PREVIEW_CI_IMAGE", "")):
        raise ValueError("TEST_PREVIEW_CI_IMAGE must be pinned by sha256 digest")


def validate_lease(request):
    key = "setupDeadline" if request["buildRequired"] else "expiresAt"
    value = request["preview"].get(key, "")
    deadline = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline <= dt.datetime.now(dt.timezone.utc):
        raise ValueError("Trusted preview reservation or lease has expired")


def read_request(env):
    filename = f"requests/{env['CI_PROJECT_ID']}-{env['CI_PIPELINE_ID']}.json"
    url = (env["CI_API_V4_URL"].rstrip("/")
           + "/projects/" + quote(CONFIG["centralProjectPath"], safe="") + "/repository/files/"
           + quote(filename, safe="") + "/raw?ref=" + quote(CONFIG["stateRef"], safe=""))
    body, _ = fetch(url, {"JOB-TOKEN": env["CI_JOB_TOKEN"]})
    request = json.loads(body)
    preview = request["preview"]
    expected = {
        "sourceProjectId": env["CI_PROJECT_ID"],
        "sourceSha": env["CI_COMMIT_SHA"],
        "sourceTag": env["CI_COMMIT_TAG"],
        "imageRepository": env["CI_REGISTRY_IMAGE"],
    }
    for key, value in expected.items():
        if str(preview.get(key, "")) != value:
            raise ValueError(f"Trusted request does not match this build: {key}")
    if type(request.get("buildRequired")) is not bool:
        raise ValueError("Trusted request must specify buildRequired")
    if request["buildRequired"]:
        expected_tag = f"preview-{env['CI_PROJECT_ID']}-{env['CI_PIPELINE_ID']}"
        if (str(preview.get("sourcePipelineId")) != env["CI_PIPELINE_ID"]
                or preview.get("buildImageTag") != expected_tag
                or request.get("status") != "pending"):
            raise ValueError("Trusted pending reservation does not match this pipeline")
    elif (request.get("status") != "active"
          or not re.fullmatch(DIGEST_PATTERN, preview.get("imageDigest", ""))):
        raise ValueError("A reused preview must already have an active verified digest")
    validate_lease(request)
    return request


def registry_digest(env, repository, tag):
    registry = env["CI_REGISTRY"]
    prefix = registry + "/"
    if not repository.startswith(prefix):
        raise ValueError("Catalog image repository does not belong to CI_REGISTRY")
    path = repository[len(prefix):]
    url = f"https://{registry}/v2/{quote(path, safe='/')}/manifests/{quote(tag, safe='')}"
    headers = {"Accept": MANIFEST_TYPES}
    try:
        body, response_headers = fetch(url, headers)
    except HTTPError as error:
        if error.code == 404:
            return None
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise ValueError("Registry must support Bearer authentication") from None
        fields = dict(re.findall(r'(\w+)="([^"\\]*)"', challenge[7:]))
        realm = fields.get("realm", "")
        if (urlparse(realm).scheme != "https"
                or urlparse(realm).netloc != urlparse(env["CI_API_V4_URL"]).netloc):
            raise ValueError("Registry authentication realm must be this GitLab instance")
        auth = base64.b64encode(
            f"{env['CI_REGISTRY_USER']}:{env['CI_REGISTRY_PASSWORD']}".encode()
        ).decode()
        separator = "&" if urlparse(realm).query else "?"
        token_body, _ = fetch(realm + separator + urlencode({
            "service": fields.get("service", "container_registry"),
            "scope": f"repository:{path}:pull",
        }), {"Authorization": "Basic " + auth})
        token_info = json.loads(token_body)
        token = token_info.get("token") or token_info["access_token"]
        headers["Authorization"] = "Bearer " + token
        try:
            body, response_headers = fetch(url, headers)
        except HTTPError as authenticated_error:
            if authenticated_error.code == 404:
                return None
            raise
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if response_headers.get("Docker-Content-Digest") != digest:
        raise ValueError("Registry manifest content does not match its digest header")
    return digest


def build_version(env):
    # Numeric prerelease component fits .NET and avoids treating dotted Git tags
    # as an assembly/package version. A catalog-specific build may override it.
    version = env.get("TEST_BUILD_VERSION") or f"0.0.0-test.{env['CI_PIPELINE_IID']}"
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", version):
        raise ValueError("TEST_BUILD_VERSION must be an application-compatible version")
    return version


def run(env):
    validate_identity(env)
    request = read_request(env)
    preview = request["preview"]
    repository, tag = preview["imageRepository"], preview["buildImageTag"]
    if not request["buildRequired"]:
        digest = preview["imageDigest"]
        print(f"Reusing {preview['id']}; lease remains {preview['expiresAt']}")
    else:
        digest = registry_digest(env, repository, tag)
        if digest:
            print("Reusing this pipeline's already published immutable build tag")
        else:
            dockerfile = Path(env.get("DOCKERFILE") or "Dockerfile")
            context = Path(env.get("TEST_DOCKER_CONTEXT") or ".")
            cwd = Path.cwd().resolve()
            for candidate in (dockerfile, context):
                if not candidate.resolve().is_relative_to(cwd):
                    raise ValueError("Dockerfile and build context must be in the source checkout")
            subprocess.run([
                "docker", "login", "--username", env["CI_REGISTRY_USER"],
                "--password-stdin", env["CI_REGISTRY"],
            ], input=env["CI_REGISTRY_PASSWORD"], text=True, check=True)
            subprocess.run([
                "docker", "build", "--file", str(dockerfile), "--build-arg",
                "APP_VERSION=" + build_version(env), "--tag", f"{repository}:{tag}", str(context),
            ], check=True)
            validate_lease(request)
            subprocess.run(["docker", "push", f"{repository}:{tag}"], check=True)
            digest = registry_digest(env, repository, tag)
            if not digest:
                raise ValueError("Pushed image is not available in the registry")
    validate_lease(request)
    artifact = {
        "sourceProjectId": env["CI_PROJECT_ID"],
        "sourcePipelineId": env["CI_PIPELINE_ID"],
        "sourceSha": env["CI_COMMIT_SHA"],
        "sourceTag": env["CI_COMMIT_TAG"],
        "imageRepository": repository,
        "buildImageTag": tag,
        "imageDigest": digest,
        "previewId": preview["id"],
        "reused": not request["buildRequired"],
    }
    Path("preview-build.json").write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"Preview image: {repository}@{digest}")
    # Deliberately omit the movable source-tag alias; only the immutable build tag
    # is published. No infrastructure credential exists in this job.


def report(env):
    validate_identity(env)
    request = read_request(env)
    if request["status"] != "active":
        raise ValueError("Preview is not active")
    preview = request["preview"]
    print(f"Preview: {preview['id']}")
    if preview.get("host"):
        print(f"URL: https://{preview['host']}/")
    print(f"Namespace: {preview['namespace']}")
    print(f"Expires at: {preview['expiresAt']}")
    if preview.get("environmentId"):
        server = env["CI_API_V4_URL"].split("/api/", 1)[0]
        print(f"Environment / Stop: {server}/{CONFIG['centralProjectPath']}/-/environments/{preview['environmentId']}")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "report":
            report(os.environ)
        elif len(sys.argv) == 1:
            run(os.environ)
        else:
            raise ValueError("Usage: test_preview_ci.py [report]")
    except (KeyError, ValueError, OSError, HTTPError, subprocess.CalledProcessError) as error:
        print(f"Preview build failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
