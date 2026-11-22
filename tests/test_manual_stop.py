"""Authenticated source-project manual stop requests, using actual cleanup behavior."""
import contextlib
import copy
import datetime as dt
import io
import json
import re
import unittest
from unittest.mock import patch

from test_preview import AT, CATALOG, SOURCE, FakeArgo, MemoryStore, application_for, preview


CALLER_PIPELINE = "90001"
CALLER_SHA = "c" * 40
CALLER_USER = 41
CENTRAL_PIPELINE = "999"
STOP_ENV = {"PREVIEW_ACTION": "stop", "PREVIEW_SOURCE_PROJECT_ID": "123",
            "PREVIEW_SOURCE_PIPELINE_ID": CALLER_PIPELINE, "PREVIEW_SOURCE_SHA": CALLER_SHA,
            "PREVIEW_TARGET_PIPELINE_ID": SOURCE["sourcePipelineId"]}


class ManualStopAPI:
    def __init__(self):
        self.calls = []
        self.identity = {"name": "stop:requested", "user": {"id": CALLER_USER}}
        self.pipeline = {"source": "pipeline", "id": int(CENTRAL_PIPELINE)}
        self.upstream = {"id": int(CALLER_PIPELINE), "project_id": 123, "sha": CALLER_SHA,
                         "source": "web", "ref": "develop", "user": {"id": CALLER_USER}}
        self.bridges = [{"name": "stop:test", "downstream_pipeline": {"id": int(CENTRAL_PIPELINE)}}]
        self.member = {"id": CALLER_USER, "access_level": 30}
        self.target_tag = None

    def current_identity(self):
        return "777", CENTRAL_PIPELINE

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data))
        if method == "GET" and re.fullmatch(r"/projects/[0-9]+/pipelines/[0-9]+", path):
            return copy.deepcopy(self.upstream)
        if method == "GET" and path == f"/projects/123/members/all/{CALLER_USER}":
            if self.member is None:
                if kwargs.get("absent"):
                    return None
                raise AssertionError("Missing membership should be requested with absent=True")
            return copy.deepcopy(self.member)
        if method == "GET" and "/repository/tags/" in path:
            return copy.deepcopy(self.target_tag)
        raise AssertionError((method, path))

    def pages(self, path):
        self.calls.append(("GET", path, None))
        if path == f"/projects/123/pipelines/{CALLER_PIPELINE}/bridges":
            return copy.deepcopy(self.bridges)
        raise AssertionError(path)


class ManualStopTests(unittest.TestCase):
    def fixture(self, alias=None):
        value = preview.make_preview(SOURCE, CATALOG, AT, "b" * 40)
        value.update(imageDigest="sha256:" + "d" * 64, registryRepositoryId="55",
                     expiresAt=preview.timestamp(AT + dt.timedelta(hours=12)))
        value["provisioning"] = preview.provisioning_values(value, CATALOG)
        watched = preview.record_path("previews", value)
        request = {"status": "active", "buildRequired": False, "recordPath": watched, "preview": copy.deepcopy(value)}
        entries = {watched: {"status": "active", "preview": copy.deepcopy(value)},
                   preview.request_path(SOURCE): copy.deepcopy(request)}
        env = dict(STOP_ENV)
        if alias:
            entries[f"requests/123-{alias}.json"] = copy.deepcopy(request)
            env["PREVIEW_TARGET_PIPELINE_ID"] = alias
        store, api, argo = MemoryStore(entries), ManualStopAPI(), FakeArgo()
        argo.value = application_for(value)
        lifecycle = preview.Lifecycle(api, store, argo, "777", CENTRAL_PIPELINE)
        return lifecycle, env, value

    def run_stop(self, lifecycle, env):
        with patch.object(preview, "now", return_value=AT + dt.timedelta(hours=1)):
            return lifecycle.stop_requested(env)

    def reject(self, mutate=None, env_changes=None, remove_env=()):
        lifecycle, env, value = self.fixture()
        if mutate:
            mutate(lifecycle, env, value)
        if env_changes:
            env.update(env_changes)
        for key in remove_env:
            env.pop(key, None)
        original = copy.deepcopy(lifecycle.store.data)
        with self.assertRaises(preview.Failure):
            self.run_stop(lifecycle, env)
        self.assertEqual(lifecycle.store.data, original)
        self.assertFalse(lifecycle.store.writes)
        self.assertTrue(all(method == "GET" for method, _, _ in lifecycle.api.calls))

    def test_developer_can_request_stop_from_manual_source_branch_pipeline_without_a_tag(self):
        lifecycle, env, value = self.fixture()
        self.assertNotIn("PREVIEW_SOURCE_TAG", env)
        self.assertNotEqual(env["PREVIEW_SOURCE_PIPELINE_ID"], env["PREVIEW_TARGET_PIPELINE_ID"])
        result = self.run_stop(lifecycle, env)
        self.assertEqual(result["status"], "cleaning")
        self.assertEqual(result["preview"]["id"], value["id"])
        self.assertNotIn(preview.record_path("previews", value), lifecycle.store.data)
        cleanup = lifecycle.store.read(preview.record_path("cleanup", value))
        self.assertEqual(cleanup["phase"], "application")
        self.assertEqual(preview.occupied_project_previews(lifecycle.all_records(), "123"), {value["id"]})
        self.assertIn(("GET", f"/projects/123/members/all/{CALLER_USER}", None), lifecycle.api.calls)

    def test_changed_or_deleted_target_tag_is_not_fetched_before_or_during_teardown(self):
        for tag in (None, {"commit": {"id": "e" * 40}}):
            with self.subTest(tag_deleted=tag is None):
                lifecycle, env, _ = self.fixture()
                lifecycle.api.target_tag = tag
                self.assertEqual(self.run_stop(lifecycle, env)["status"], "cleaning")
                self.assertFalse(any("/repository/tags/" in path for _, path, _ in lifecycle.api.calls))
                self.assertFalse(any(path.endswith("/pipelines/" + SOURCE["sourcePipelineId"]) for _, path, _ in lifecycle.api.calls))

    def test_reporter_and_missing_source_membership_are_rejected_without_writes(self):
        for member in ({"access_level": 20}, None):
            with self.subTest(member=member):
                self.reject(lambda lifecycle, env, value: setattr(lifecycle.api, "member", member))

    def test_forged_downstream_pipeline_link_is_rejected(self):
        self.reject(lambda lifecycle, env, value: setattr(lifecycle.api, "bridges", [{"name": "stop:test", "downstream_pipeline": {"id": 111}}]))

    def test_other_bridge_job_cannot_authorize_a_stop_request(self):
        for name in ("prepare:test", "deploy:test", "other-job"):
            with self.subTest(name=name):
                self.reject(lambda lifecycle, env, value: setattr(lifecycle.api, "bridges", [{"name": name, "downstream_pipeline": {"id": int(CENTRAL_PIPELINE)}}]))

    def test_non_web_source_pipeline_cannot_request_manual_stop(self):
        for source in ("push", "schedule", "api", "pipeline"):
            with self.subTest(source=source):
                self.reject(lambda lifecycle, env, value: lifecycle.api.upstream.update(source=source))

    def test_requesting_pipeline_id_project_and_sha_must_match_real_api_identity(self):
        for change in ({"id": 90002}, {"project_id": 456}, {"sha": "e" * 40}):
            with self.subTest(change=change):
                self.reject(lambda lifecycle, env, value: lifecycle.api.upstream.update(change))

    def test_forwarded_source_identity_must_match_real_requesting_pipeline(self):
        for change in ({"PREVIEW_SOURCE_PROJECT_ID": "456"}, {"PREVIEW_SOURCE_PIPELINE_ID": "90002"}, {"PREVIEW_SOURCE_SHA": "e" * 40}):
            with self.subTest(change=change):
                self.reject(env_changes=change)

    def test_only_the_dedicated_central_job_and_pipeline_source_are_allowed(self):
        self.reject(lambda lifecycle, env, value: lifecycle.api.identity.update(name="register"))
        self.reject(lambda lifecycle, env, value: lifecycle.api.pipeline.update(source="web"))
        self.reject(env_changes={"PREVIEW_ACTION": "register"})

    def test_unknown_or_noncanonical_target_pipeline_ids_are_rejected(self):
        for target in ("98765", "", "0", "01", "-1", "1.5", "word", " 45678", "45678 "):
            with self.subTest(target=target):
                self.reject(env_changes={"PREVIEW_TARGET_PIPELINE_ID": target})
        self.reject(remove_env=("PREVIEW_TARGET_PIPELINE_ID",))

    def test_cross_project_target_request_cannot_be_stopped(self):
        def mutate(lifecycle, env, value):
            request = lifecycle.store.data[preview.request_path(SOURCE)]
            request["preview"]["sourceProjectId"] = "456"
            request["preview"]["id"] = "example-app-456-45678"
            request["preview"]["namespace"] = "test-example-app-456-45678"
        self.reject(mutate)

    def test_stored_target_preview_identity_must_be_canonical(self):
        changes = ({"id": "example-app-999-45678"}, {"namespace": "test-example-app-456-45678"},
                   {"sourcePipelineId": "01"}, {"sourcePipelineId": "45679"}, {"service": "different-service"})
        for change in changes:
            with self.subTest(change=change):
                self.reject(lambda lifecycle, env, value: lifecycle.store.data[preview.request_path(SOURCE)]["preview"].update(change))

    def test_request_alias_resolves_its_canonical_original_preview(self):
        lifecycle, env, value = self.fixture(alias="88888")
        result = self.run_stop(lifecycle, env)
        self.assertEqual(result["status"], "cleaning")
        self.assertEqual(result["preview"]["sourcePipelineId"], SOURCE["sourcePipelineId"])
        self.assertEqual(result["preview"]["id"], value["id"])
        self.assertEqual(lifecycle.store.read("requests/123-88888.json")["status"], "cleaning")
        self.assertEqual(lifecycle.store.read(preview.request_path(SOURCE))["status"], "cleaning")

    def test_retry_of_requested_cleanup_remains_bounded_and_reserved(self):
        lifecycle, env, value = self.fixture()
        self.run_stop(lifecycle, env)
        count = len(lifecycle.store.writes)
        result = self.run_stop(lifecycle, env)
        self.assertEqual(result["status"], "cleaning")
        self.assertEqual(len(lifecycle.store.writes), count)
        self.assertEqual(preview.occupied_project_previews(lifecycle.all_records(), "123"), {value["id"]})

    def test_missing_tracked_state_does_not_report_false_stop_success(self):
        def mutate(lifecycle, env, value):
            del lifecycle.store.data[preview.record_path("previews", value)]
        self.reject(mutate)

    def test_stopped_tombstone_is_noop_and_cannot_stop_a_newer_reused_environment(self):
        lifecycle, env, value = self.fixture()
        old = lifecycle.store.data[preview.request_path(SOURCE)]
        old.update(status="stopped", buildRequired=False)
        del lifecycle.store.data[preview.record_path("previews", value)]
        newer = copy.deepcopy(value)
        newer.update(sourcePipelineId="88888", id="example-app-123-88888", namespace="test-example-app-123-88888")
        new_path = preview.record_path("previews", newer)
        lifecycle.store.data[new_path] = {"status": "active", "preview": newer}
        lifecycle.argo.value = application_for(newer)
        before = copy.deepcopy(lifecycle.store.data)
        result = self.run_stop(lifecycle, env)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(lifecycle.store.data, before)
        self.assertFalse(lifecycle.store.writes)
        self.assertFalse(lifecycle.argo.calls)

    def test_stopped_tombstone_still_cannot_leak_cross_project_identity(self):
        def mutate(lifecycle, env, value):
            request = lifecycle.store.data[preview.request_path(SOURCE)]
            request.update(status="stopped", buildRequired=False)
            request["preview"].update(sourceProjectId="456", id="example-app-456-45678", namespace="test-example-app-456-45678")
        self.reject(mutate)

    def test_legacy_stop_command_cannot_bypass_dedicated_request_authorization(self):
        lifecycle, env, _ = self.fixture()
        lifecycle.api.identity["name"] = "stop"
        before = copy.deepcopy(lifecycle.store.data)
        with patch.dict(preview.os.environ, env, clear=True), patch.object(preview.sys, "argv", ["preview.py", "stop"]):
            with patch.object(preview, "GitLab", return_value=lifecycle.api), patch.object(preview, "Store", return_value=lifecycle.store), patch.object(preview, "ArgoCD", return_value=lifecycle.argo):
                with self.assertRaisesRegex(preview.Failure, "Only a registration pipeline"):
                    preview.main()
        self.assertEqual(lifecycle.store.data, before)
        self.assertFalse(lifecycle.store.writes)
        self.assertFalse(lifecycle.argo.calls)

    def test_cli_stop_request_dispatches_and_reports_real_cleanup_progress(self):
        lifecycle, env, value = self.fixture()
        output = io.StringIO()
        with patch.dict(preview.os.environ, env, clear=True), patch.object(preview.sys, "argv", ["preview.py", "stop-request"]):
            with patch.object(preview, "GitLab", return_value=lifecycle.api), patch.object(preview, "Store", return_value=lifecycle.store), patch.object(preview, "ArgoCD", return_value=lifecycle.argo):
                with patch.object(preview, "now", return_value=AT + dt.timedelta(hours=1)), contextlib.redirect_stdout(output):
                    preview.main()
        results = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(results)
        self.assertEqual(results[-1]["id"], value["id"])
        self.assertEqual(results[-1]["status"], "cleaning")


if __name__ == "__main__":
    unittest.main()
