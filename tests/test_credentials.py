"""Verify CLI credential selection before any API request is made."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


from support import load_module

preview = load_module("test_credentials")


class IdentityProbe(Exception):
    pass


class CredentialTests(unittest.TestCase):
    def test_malformed_header_credentials_fail_without_echoing_the_value(self):
        value = "synthetic-token\nInjected: do-not-log"
        for token, job in ((value, None), ("test-api-token", value)):
            with self.subTest(job=job is not None):
                with self.assertRaises(preview.Failure) as raised:
                    preview.GitLab(token, job)
                self.assertNotIn(value, str(raised.exception))


    def assert_startup_credential(self, variables, expected, failure=IdentityProbe):
        # Stop before identity lookup: only assert which credential the real CLI
        # passes to its API client, without making a network request.
        with patch.dict(os.environ, {"CI_JOB_TOKEN": "test-job-token", **variables}, clear=True):
            with patch.object(preview.sys, "argv", ["preview.py", "prepare"]):
                with patch.object(preview.GitLab, "current_identity", side_effect=failure):
                    with patch.object(preview, "GitLab", wraps=preview.GitLab) as client:
                        with self.assertRaises(failure):
                            preview.main()
                        client.assert_called_once_with(expected, "test-job-token")

    def test_cli_reuses_helm_account_token(self):
        self.assert_startup_credential({"HELM_USER": "existing-account", "HELM_TAG_TOKEN": "test-existing-token"}, "test-existing-token")

    def test_explicit_preview_token_keeps_precedence(self):
        self.assert_startup_credential({"PREVIEW_GITLAB_TOKEN": "test-explicit-token", "HELM_TAG_TOKEN": "test-existing-token"}, "test-explicit-token")

    def test_empty_preview_token_uses_existing_helm_token(self):
        self.assert_startup_credential({"PREVIEW_GITLAB_TOKEN": "", "HELM_TAG_TOKEN": "test-existing-token"}, "test-existing-token")

    def test_invalid_explicit_token_does_not_retry_with_helm(self):
        self.assert_startup_credential({"PREVIEW_GITLAB_TOKEN": "test-invalid-token", "HELM_TAG_TOKEN": "test-existing-token"}, "test-invalid-token", failure=preview.Failure)

    def test_username_alone_cannot_authenticate(self):
        with patch.dict(os.environ, {"HELM_USER": "existing-account", "CI_JOB_TOKEN": "test-job-token"}, clear=True):
            with patch.object(preview.sys, "argv", ["preview.py", "prepare"]):
                with self.assertRaisesRegex(preview.Failure, "HELM_TAG_TOKEN"):
                    preview.main()


if __name__ == "__main__":
    unittest.main()
