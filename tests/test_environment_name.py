import datetime as dt
import importlib.util
from pathlib import Path
import unittest

from support import load_module

preview = load_module("test_environment_name")


class EnvironmentNameTests(unittest.TestCase):
    def test_duplicate_source_tag_pipelines_share_environment_but_not_resource_names(self):
        source = {"sourceProjectId": "123", "sourcePipelineId": "45678", "sourceSha": "206" + "a" * 37, "sourceTag": "test-demo"}
        catalog = {"service": "example-app", "image": {"repository": "cr.company.example/company/example-app"}, "chart": {"approvedRevision": "b" * 40}, "provisioning": {"approvedRevision": "d" * 40}, "endpoint": {"type": "http", "hostname": "example-app"}}
        at = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc)
        original = preview.make_preview(source, catalog, at, "c" * 40)
        retry = preview.make_preview(dict(source, sourcePipelineId="99999"), catalog, at, "c" * 40)
        self.assertEqual(original["environmentName"], "test/example-app-123-" + source["sourceSha"])
        self.assertEqual(retry["environmentName"], original["environmentName"])
        self.assertNotEqual(retry["id"], original["id"])
        self.assertNotEqual(retry["namespace"], original["namespace"])

    def test_environment_names_distinguish_projects_and_commits(self):
        original = {"service": "example-app", "sourceProjectId": "123", "sourceTag": "test0.wor1", "sourceSha": "206" + "a" * 37}
        name = preview.environment_name(original)
        self.assertEqual(name, "test/example-app-123-" + original["sourceSha"])
        self.assertNotEqual(name, preview.environment_name(dict(original, sourceProjectId="456")))
        self.assertNotEqual(name, preview.environment_name(dict(original, sourceSha="207" + "a" * 37)))
        self.assertEqual(name, preview.environment_name(dict(original, sourceTag="test-another")))

    def test_environment_rejects_unreviewed_service_or_invalid_sha(self):
        original = {"service": "example-app", "sourceProjectId": "123", "sourceTag": "test-demo", "sourceSha": "206" + "a" * 37}
        for changed in (dict(original, service="payment"), dict(original, sourceSha="short"), dict(original, sourceProjectId="../123")):
            with self.assertRaises(preview.Failure):
                preview.environment_name(changed)

    def test_stopped_request_cannot_stop_newer_lease_reusing_environment(self):
        class Store:
            def read(self, path):
                return {"status": "stopped", "preview": {"id": "old-preview"}}
        lifecycle = preview.Lifecycle(None, Store(), None, "777", "999")
        with self.assertRaises(preview.Failure):
            lifecycle.stop({"PREVIEW_SOURCE_PROJECT_ID": "123", "PREVIEW_SOURCE_PIPELINE_ID": "45678"})


if __name__ == "__main__":
    unittest.main()
