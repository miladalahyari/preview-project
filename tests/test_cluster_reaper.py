"""Cluster reaper identity and Application absence must fail closed."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from test_preview import preview


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.value).encode()


class ClusterReaperTests(unittest.TestCase):
    def test_cluster_identity_requires_matching_protected_code_and_maintainer(self):
        revision = "a" * 40
        responses = [{"path_with_namespace": "company/previews"},
                     {"protected": True, "commit": {"id": revision}},
                     {"id": 36}, {"access_level": 40}]
        api = preview.GitLab("test-token")
        with patch.dict(os.environ, {"PREVIEW_CODE_REVISION": revision}, clear=True):
            with patch.object(api, "request", side_effect=responses) as call:
                self.assertEqual(api.cluster_reaper_identity(), ("200", "0"))
                self.assertEqual(call.call_args_list[-1].args[1], "/projects/200/members/all/36")
        for change in ({1: {"protected": False, "commit": {"id": revision}}},
                       {3: {"access_level": 30}}):
            values = list(responses)
            for index, value in change.items():
                values[index] = value
            with self.subTest(change=change), patch.dict(os.environ, {"PREVIEW_CODE_REVISION": revision}, clear=True):
                with patch.object(api, "request", side_effect=values), self.assertRaises(preview.Failure):
                    api.cluster_reaper_identity()
        with patch.dict(os.environ, {"PREVIEW_CODE_REVISION": "b" * 40}, clear=True):
            with patch.object(api, "request", side_effect=responses), self.assertRaises(preview.Failure):
                api.cluster_reaper_identity()

    def test_cluster_observer_accepts_only_exact_application_not_found(self):
        client = preview.KubernetesArgo.__new__(preview.KubernetesArgo)
        client.token = "test-token"
        name = "example-app-123-45678"
        class Opener:
            def __init__(self, result):
                self.result = result
            def open(self, request, timeout):
                self.request = request
                if isinstance(self.result, Exception):
                    raise self.result
                return Response(self.result)
        valid = {"kind": "Application", "metadata": {"name": name}}
        client.opener = Opener(valid)
        self.assertEqual(client.application(name), valid)
        self.assertEqual(client.opener.request.full_url,
                         "https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/" + name)
        status = {"reason": "NotFound", "details": {"name": name, "kind": "applications", "group": "argoproj.io"}}
        for body, missing in ((status, True), ({"reason": "NotFound"}, False),
                              ({"reason": "NotFound", "details": dict(status["details"], name="other")}, False),
                              ({"message": "proxy 404"}, False)):
            with self.subTest(body=body):
                client.opener = Opener(urllib.error.HTTPError("https://kubernetes.default.svc", 404, "", {}, io.BytesIO(json.dumps(body).encode())))
                if missing:
                    self.assertIsNone(client.application(name))
                else:
                    with self.assertRaises(preview.Failure):
                        client.application(name)
        client.opener = Opener(urllib.error.HTTPError("https://kubernetes.default.svc", 403, "", {}, io.BytesIO()))
        with self.assertRaises(preview.Failure):
            client.application(name)

    def test_bootstrap_uses_one_protected_revision_for_code_and_catalog(self):
        import importlib.util
        module_path = Path(__file__).resolve().parents[1] / "scripts/cluster_reaper_bootstrap.py"
        spec = importlib.util.spec_from_file_location("cluster_reaper_bootstrap", module_path)
        module = importlib.util.module_from_spec(spec)
        from support import load_module
        module = load_module("cluster_reaper_bootstrap", "cluster_reaper_bootstrap.py")
        revision = "a" * 40
        paths = []
        def fetch(path, token, raw=False):
            paths.append(path)
            if path.endswith("/projects/200"):
                return {"path_with_namespace": "company/previews"}
            if path.endswith("/repository/branches/main"):
                return {"protected": True, "commit": {"id": revision}}
            if "/repository/tree?" in path:
                return [{"name": "example-app.json", "type": "blob"}]
            return b"print('safe test')\n" if "scripts%2Fpreview.py" in path else b"{}\n"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(module, "ROOT", Path(directory)), patch.dict(os.environ, {"PREVIEW_GITLAB_TOKEN": "test-token"}, clear=True):
                with patch.object(module, "fetch", side_effect=fetch), patch.object(module.subprocess, "run") as run:
                    module.main()
            self.assertTrue((Path(directory) / "scripts/preview.py").exists())
            self.assertTrue((Path(directory) / "catalog/example-app.json").exists())
        self.assertTrue(all("ref=" + revision in path for path in paths if "/raw?" in path))
        self.assertEqual(run.call_args.kwargs["env"]["PREVIEW_CODE_REVISION"], revision)


if __name__ == "__main__":
    unittest.main()
