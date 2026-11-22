import io
import json
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

from test_preview import preview


class ArgoObserverTests(unittest.TestCase):
    def observer(self, outcome):
        observer = preview.ArgoCD(preview.ARGOCD_SERVER, "test-observer-token")
        observer.opener = Mock()
        if isinstance(outcome, Exception):
            observer.opener.open.side_effect = outcome
        else:
            observer.opener.open.return_value = io.BytesIO(json.dumps(outcome).encode())
        return observer

    def error(self, status, body):
        return urllib.error.HTTPError("https://argocd.company.example/api/v1/applications/example-app-123-45678", status, "test", {}, io.BytesIO(body))

    def test_observer_uses_only_project_scoped_authenticated_get(self):
        observer = self.observer({"metadata": {"name": "example-app-123-45678"}})
        self.assertEqual(observer.application("example-app-123-45678")["metadata"]["name"], "example-app-123-45678")
        request = observer.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, preview.ARGOCD_SERVER + "/api/v1/applications/example-app-123-45678?project=previews&appNamespace=argocd")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-observer-token")

    def test_only_actual_argocd_not_found_is_absence(self):
        observer = self.observer(self.error(404, b'{"code":5,"message":"application not found"}'))
        self.assertIsNone(observer.application("example-app-123-45678"))
        for body in (b"<html>proxy route missing</html>", b'{"message":"not found"}', b'{"code":7}'):
            with self.subTest(body=body):
                with self.assertRaises(preview.Failure):
                    self.observer(self.error(404, body)).application("example-app-123-45678")

    def test_authentication_and_network_failures_never_mean_deleted(self):
        failures = [self.error(status, b'{"code":5}') for status in (401, 403, 500)]
        failures += [urllib.error.URLError("offline"), TimeoutError()]
        for error in failures:
            with self.subTest(error=type(error).__name__), self.assertRaises(preview.Failure) as raised:
                self.observer(error).application("example-app-123-45678")
            self.assertNotIn("test-observer-token", str(raised.exception))

    def test_credential_redirects_are_rejected(self):
        handler = preview.NoArgoRedirect()
        request = urllib.request.Request(preview.ARGOCD_SERVER + "/api/v1/applications/a", headers={"Authorization": "Bearer test-observer-token"})
        for destination in ("https://elsewhere.example/", "http://argocd.company.example/", preview.ARGOCD_SERVER + "/login"):
            with self.subTest(destination=destination), self.assertRaises(preview.Failure):
                handler.redirect_request(request, None, 302, "Found", {}, destination)

    def test_observer_rejects_unknown_origins_unsafe_names_and_bad_token(self):
        for server in ("http://argocd.company.example", "https://argocd.company.example.attacker.example", "https://user:password@argocd.company.example"):
            with self.subTest(server=server), self.assertRaises(preview.Failure):
                preview.ArgoCD(server, "token")
        with self.assertRaises(preview.Failure):
            preview.ArgoCD(preview.ARGOCD_SERVER, "token\nInjected: header")
        observer = self.observer({})
        for name in ("../production", "production-app", "example-app-" + "a" * 64):
            with self.subTest(name=name), self.assertRaises(preview.Failure):
                observer.application(name)
        observer.opener.open.assert_not_called()
