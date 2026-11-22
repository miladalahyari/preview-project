import importlib.util
import copy
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "preview_ci", ROOT / "tools/test_preview_ci.py"
)
CLIENT = importlib.util.module_from_spec(SPEC)
with patch.object(Path, "read_text", return_value=(ROOT / "tests/fixtures/platform.json").read_text()):
    SPEC.loader.exec_module(CLIENT)


class GitLabLoader(yaml.SafeLoader):
    pass


GitLabLoader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


def load_yaml(path):
    return yaml.load(path.read_text(), Loader=GitLabLoader)


class PreviewBuildTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "CI_PROJECT_ID": "123", "CI_PIPELINE_ID": "45678", "CI_PIPELINE_IID": "12",
            "CI_COMMIT_SHA": "206" + "a" * 37, "CI_COMMIT_TAG": "test-demo",
            "CI_REGISTRY_IMAGE": "cr.example.test/company/example-app",
            "CI_REGISTRY": "cr.example.test", "CI_REGISTRY_USER": "job",
            "CI_REGISTRY_PASSWORD": "test-only-password", "CI_JOB_TOKEN": "test-only-token",
            "CI_API_V4_URL": "https://gitlab.example.test/api/v4",
            "TEST_PREVIEW_CI_IMAGE": "registry/tools@sha256:" + "b" * 64,
        }
        self.request = {
            "status": "pending", "buildRequired": True,
            "preview": {
                "id": "example-app-123-45678", "sourceProjectId": "123",
                "sourcePipelineId": "45678", "sourceSha": self.env["CI_COMMIT_SHA"],
                "sourceTag": "test-demo", "imageRepository": self.env["CI_REGISTRY_IMAGE"],
                "buildImageTag": "preview-123-45678",
                "setupDeadline": (CLIENT.dt.datetime.now(CLIENT.dt.timezone.utc)
                                  + CLIENT.dt.timedelta(minutes=30)).isoformat(),
            },
        }

    def fetch_request(self):
        return patch.object(CLIENT, "fetch", return_value=(json.dumps(self.request).encode(), {}))

    def test_supported_dotted_tags_and_length(self):
        for tag in ("test-demo", "test0.wor1", "test-alice", "test.work1"):
            with self.subTest(tag=tag):
                CLIENT.validate_identity(dict(self.env, CI_COMMIT_TAG=tag))
        for tag in ("test", "test-UPPER", "test/a", "test a", "test-" + "a" * 124):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                CLIENT.validate_identity(dict(self.env, CI_COMMIT_TAG=tag))

    def test_tools_image_must_be_immutable(self):
        with self.assertRaises(ValueError):
            CLIENT.validate_identity(dict(self.env, TEST_PREVIEW_CI_IMAGE="tools:latest"))

    def test_source_build_refuses_infrastructure_write_credentials(self):
        for key in ("HELM_TAG_TOKEN", "PREVIEW_GITLAB_TOKEN", "ARGOCD_AUTH_TOKEN", "KUBECONFIG", "KUBE_TOKEN"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                CLIENT.validate_identity(dict(self.env, **{key: "must-not-be-exposed"}))

    def test_expired_or_timezone_less_reservations_cannot_build(self):
        for deadline in ("2000-01-01T00:00:00Z", "2100-01-01T00:00:00"):
            self.request["preview"]["setupDeadline"] = deadline
            with self.subTest(deadline=deadline), self.fetch_request(), self.assertRaises(ValueError):
                CLIENT.read_request(self.env)

    def test_trusted_reservation_must_match_current_pipeline(self):
        self.request["preview"]["sourcePipelineId"] = "999"
        with self.fetch_request(), self.assertRaises(ValueError):
            CLIENT.read_request(self.env)

    def test_trusted_reservation_must_match_image_repository(self):
        self.request["preview"]["imageRepository"] = "cr.example.test/another/project"
        with self.fetch_request(), self.assertRaises(ValueError):
            CLIENT.read_request(self.env)

    def test_active_request_reuses_original_pipeline_and_does_not_publish(self):
        self.request.update(status="active", buildRequired=False)
        self.request["preview"].update(
            sourcePipelineId="45600", buildImageTag="preview-123-45600",
            imageDigest="sha256:" + "d" * 64,
            expiresAt=(CLIENT.dt.datetime.now(CLIENT.dt.timezone.utc)
                       + CLIENT.dt.timedelta(hours=12)).isoformat(),
        )
        with tempfile.TemporaryDirectory() as directory:
            original = Path.cwd()
            try:
                os.chdir(directory)
                with self.fetch_request(), patch.object(CLIENT.subprocess, "run") as docker:
                    CLIENT.run(self.env)
                    docker.assert_not_called()
                artifact = json.loads(Path("preview-build.json").read_text())
                self.assertTrue(artifact["reused"])
                self.assertEqual(artifact["buildImageTag"], "preview-123-45600")
            finally:
                os.chdir(original)

    def test_retry_reuses_existing_pipeline_image(self):
        digest = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as directory:
            original = Path.cwd()
            try:
                os.chdir(directory)
                with self.fetch_request(), patch.object(CLIENT, "registry_digest", return_value=digest):
                    with patch.object(CLIENT.subprocess, "run") as docker:
                        CLIENT.run(self.env)
                        docker.assert_not_called()
                self.assertEqual(json.loads(Path("preview-build.json").read_text())["imageDigest"], digest)
            finally:
                os.chdir(original)

    def test_registry_digest_checks_actual_manifest_bytes(self):
        manifest = b'{"schemaVersion":2}'
        digest = "sha256:" + CLIENT.hashlib.sha256(manifest).hexdigest()
        with patch.object(CLIENT, "fetch", return_value=(manifest, {"Docker-Content-Digest": digest})):
            self.assertEqual(CLIENT.registry_digest(self.env, self.env["CI_REGISTRY_IMAGE"], "preview-123-45678"), digest)
        with patch.object(CLIENT, "fetch", return_value=(manifest, {"Docker-Content-Digest": "sha256:" + "0" * 64})):
            with self.assertRaises(ValueError):
                CLIENT.registry_digest(self.env, self.env["CI_REGISTRY_IMAGE"], "preview-123-45678")

    def test_registry_refuses_credential_exfiltration_via_auth_realm(self):
        error = HTTPError("https://cr.example.test", 401, "Unauthorized", {
            "WWW-Authenticate": 'Bearer realm="https://untrusted.test/token",service="registry"',
        }, io.BytesIO())
        with patch.object(CLIENT, "fetch", side_effect=error), self.assertRaises(ValueError):
            CLIENT.registry_digest(self.env, self.env["CI_REGISTRY_IMAGE"], "preview-123-45678")

    def test_build_version_is_independent_of_source_tag(self):
        self.assertEqual(CLIENT.build_version(self.env), "0.0.0-test.12")
        with self.assertRaises(ValueError):
            CLIENT.build_version(dict(self.env, TEST_BUILD_VERSION="test0.wor1"))

