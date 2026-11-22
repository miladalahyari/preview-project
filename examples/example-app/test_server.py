import unittest
from unittest.mock import patch
from server import response


class AppTests(unittest.TestCase):
    def test_health_endpoints(self):
        for path in ("/health", "/healthz"):
            self.assertEqual(response(path), (200, {"status": "ok"}))

    def test_home_reports_build_version_without_exposing_environment(self):
        with patch.dict("os.environ", {"APP_VERSION": "1.2.3", "PRIVATE_VALUE": "do-not-return"}):
            status, body = response("/")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], "1.2.3")
        self.assertNotIn("do-not-return", str(body))

    def test_unknown_routes_are_not_found(self):
        self.assertEqual(response("/missing")[0], 404)
