import copy
import base64
import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import json
import unittest
from unittest.mock import patch
import urllib.request

from support import load_module

preview = load_module("test_preview")


SOURCE = {"sourceProjectId": "123", "sourcePipelineId": "45678", "sourceSha": "206" + "a" * 37, "sourceTag": "test-demo"}
ENV = {"PREVIEW_SOURCE_PROJECT_ID": "123", "PREVIEW_SOURCE_PIPELINE_ID": "45678", "PREVIEW_SOURCE_SHA": SOURCE["sourceSha"], "PREVIEW_SOURCE_TAG": "test-demo", "PREVIEW_SERVICE": "example-app"}
AT = dt.datetime(2026, 11, 9, 8, tzinfo=dt.timezone.utc)
CATALOG = {"service": "example-app", "enabled": True, "source": {"projectId": "123", "projectPath": "company/example-app"}, "image": {"repository": "cr.company.example/company/example-app"}, "chart": {"repository": "https://gitlab.company.example/company/previews.git", "path": "charts/example-app", "approvedRevision": "a" * 40}, "adapter": {"imageDigestKey": "image.digest", "hostKey": "ingress.hosts[0].host"}, "endpoint": {"type": "http", "hostname": "example-app"}, "lifecycle": {"ttlHours": 12}}
CATALOG["provisioning"] = {"repository": preview.BOOTSTRAP_REPOSITORY, "path": preview.BOOTSTRAP_PATH,
                           "approvedRevision": "c" * 40, "approved": True, "dependenciesReviewed": True,
                           "registrySecretDelivery": True,
                           "networkPolicyFiles": ["provisioning/network.json"], "secretIntegrationFiles": [],
                           "requiredSecrets": ["company-registry"], "quota": {"pods": "5"}, "limits": [],
                           "resolvedResources": [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "traffic"}, "spec": {}}]}


class MemoryStore:
    def __init__(self, records=None):
        self.data = copy.deepcopy(records or {})
        self.writes = []

    def read(self, path):
        return copy.deepcopy(self.data.get(path))

    def records(self, prefix):
        return [(path, self.read(path)) for path in sorted(self.data) if path.startswith(prefix + "/")]

    def commit(self, changes, message):
        self.writes.append((copy.deepcopy(changes), message))
        for path, value in changes.items():
            if value is None:
                self.data.pop(path, None)
            else:
                self.data[path] = copy.deepcopy(value)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data))
        if path.endswith("/pipelines/999"):
            return {"sha": "b" * 40}
        if path == "/projects/123":
            return {"path_with_namespace": "company/example-app"}
        raise AssertionError((method, path))


class FakeArgo:
    def __init__(self):
        self.value = None
        self.calls = []

    def application(self, name):
        self.calls.append(name)
        return copy.deepcopy(self.value)


def application_for(value):
    bootstrap = {"preview": {key: value[key] for key in ("id", "namespace", "sourceProjectId", "expiresAt")}, "provisioning": value["provisioning"]}
    parameters = [{"name": key, "value": item, "forceString": True} for key, item in
                  (("fullnameOverride", value["id"]), ("image.digest", value["imageDigest"]), ("ingress.hosts[0].host", value["host"]))]
    sources = [{"repoURL": preview.BOOTSTRAP_REPOSITORY, "path": preview.BOOTSTRAP_PATH, "targetRevision": value["bootstrapRevision"], "helm": {"releaseName": value["id"], "values": json.dumps(bootstrap)}},
               {"repoURL": "https://gitlab.company.example/company/previews.git", "path": "charts/example-app", "targetRevision": value["chartRevision"],
                "helm": {"releaseName": value["id"], "valueFiles": ["values-test.yaml"], "parameters": parameters}}]
    return {"metadata": {"name": value["id"], "namespace": "argocd", "finalizers": ["resources-finalizer.argocd.argoproj.io"]},
            "spec": {"project": preview.ARGOCD_PROJECT, "destination": {"namespace": value["namespace"], "server": "https://kubernetes.default.svc"}, "sources": sources},
            "status": {"sync": {"status": "Synced", "revisions": [value["bootstrapRevision"], value["chartRevision"]], "comparedTo": {"sources": copy.deepcopy(sources)}}, "health": {"status": "Healthy"},
                       "resources": [{"group": "", "kind": "ConfigMap", "name": "preview-registry-ready", "namespace": value["namespace"], "status": "Synced", "health": {"status": "Healthy"}}]}}


class PreviewTests(unittest.TestCase):
    def value(self):
        value = dict(preview.make_preview(SOURCE, CATALOG, AT, "b" * 40), imageDigest="sha256:" + "d" * 64)
        value["provisioning"] = preview.provisioning_values(value, CATALOG)
        return value

    def lifecycle(self, store=None):
        lifecycle = preview.Lifecycle(FakeAPI(), store or MemoryStore(), FakeArgo(), "777", "999")
        lifecycle.verified_source = lambda env, **kwargs: dict(SOURCE)
        return lifecycle

    def test_tag_validation_and_exact_source_identity(self):
        for tag in ("test-demo", "test0.wor1", "test.work1"):
            self.assertEqual(preview.source_input(dict(ENV, PREVIEW_SOURCE_TAG=tag))["sourceTag"], tag)
        for tag in ("test", "test/Demo", "Test-demo", "test bad", "test-" + "a" * 128):
            with self.assertRaises(preview.Failure):
                preview.source_input(dict(ENV, PREVIEW_SOURCE_TAG=tag))

    def test_names_use_project_pipeline_and_sha3(self):
        value = self.value()
        self.assertEqual(value["id"], "example-app-123-45678")
        self.assertEqual(value["namespace"], "test-example-app-123-45678")
        self.assertEqual(value["host"], "test-206-example-app.company.example")

    def test_hostname_length_and_helm_limits(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["endpoint"]["hostname"] = "a" * 55
        with self.assertRaises(preview.Failure):
            preview.make_preview(SOURCE, catalog, AT, "b" * 40)
        with self.assertRaises(preview.Failure):
            preview.make_preview(dict(SOURCE, sourcePipelineId="1" * 50), CATALOG, AT, "b" * 40)

    def test_prepare_retry_preserves_pending_record(self):
        lifecycle = self.lifecycle()
        with patch.object(preview, "choose_catalog", return_value=CATALOG), patch.object(preview, "now", return_value=AT):
            first = lifecycle.prepare(ENV)
        with patch.object(preview, "choose_catalog", return_value=CATALOG), patch.object(preview, "now", return_value=AT + dt.timedelta(minutes=5)):
            second = lifecycle.prepare(ENV)
        self.assertEqual(first["preview"], second["preview"])
        self.assertTrue(second["buildRequired"])
        self.assertEqual(len(lifecycle.store.records("pending")), 1)

    def test_duplicate_tag_new_pipeline_reuses_digest_and_lease(self):
        value = dict(self.value(), imageDigest="sha256:" + "d" * 64, expiresAt=preview.timestamp(AT + dt.timedelta(hours=12)))
        path = preview.record_path("previews", value)
        lifecycle = self.lifecycle(MemoryStore({path: {"status": "active", "preview": value}}))
        later = dict(SOURCE, sourcePipelineId="88888")
        lifecycle.verified_source = lambda env: later
        with patch.object(preview, "choose_catalog", return_value=CATALOG), patch.object(preview, "now", return_value=AT + dt.timedelta(hours=1)):
            request = lifecycle.prepare(dict(ENV, PREVIEW_SOURCE_PIPELINE_ID="88888"))
        self.assertFalse(request["buildRequired"])
        self.assertEqual(request["preview"], value)
        self.assertIn("requests/123-88888.json", lifecycle.store.data)

    def test_collision_does_not_overwrite_existing_hostname(self):
        existing = self.value()
        other = dict(existing, sourceTag="test-other", sourceSha="206" + "b" * 37)
        with self.assertRaises(preview.Failure):
            preview.inspect_reservations([("previews/a.yaml", {"status": "active", "preview": existing})], other)

    def test_moved_reserved_tag_is_rejected(self):
        existing = self.value()
        other = dict(existing, sourceSha="abc" + "b" * 37)
        with self.assertRaises(preview.Failure):
            preview.inspect_reservations([("pending/a.yaml", {"status": "pending", "preview": existing})], other)

    def test_stopped_request_does_not_reactivate(self):
        request = {"status": "stopped", "preview": self.value(), "buildRequired": False}
        lifecycle = self.lifecycle(MemoryStore({preview.request_path(SOURCE): request}))
        with self.assertRaises(preview.Failure):
            lifecycle.prepare(ENV)
        self.assertFalse(lifecycle.store.writes)

    def test_cleanup_retains_reservation_until_application_disappears(self):
        value = self.value()
        lifecycle = self.lifecycle(MemoryStore({preview.record_path("previews", value): {"status": "active", "preview": value}}))
        path, record = lifecycle.begin_cleanup(preview.record_path("previews", value), {"status": "active", "preview": value}, "lease expiry")
        lifecycle.argo.value = application_for(value)
        self.assertFalse(lifecycle.advance_cleanup(path, record))
        self.assertNotIn(preview.record_path("previews", value), lifecycle.store.data)
        self.assertFalse(lifecycle.advance_cleanup(path, record))
        self.assertEqual(record["phase"], "application")
        self.assertIn(path, lifecycle.store.data)
        self.assertEqual(lifecycle.argo.calls, [value["id"], value["id"]])

    def test_cleanup_retains_watched_record_when_application_was_never_observed(self):
        value = self.value()
        watched = preview.record_path("previews", value)
        lifecycle = self.lifecycle(MemoryStore({watched: {"status": "provisioning", "preview": value}}))
        path, record = lifecycle.begin_cleanup(watched, lifecycle.store.read(watched), "manual stop")
        self.assertFalse(lifecycle.advance_cleanup(path, record))
        self.assertEqual(record["phase"], "observe")
        self.assertEqual(lifecycle.store.data[watched]["status"], "cleaning")
        self.assertIn(path, lifecycle.store.data)
        self.assertFalse(lifecycle.api.calls)

    def test_reaper_reports_stuck_application_without_forcing_deletion(self):
        value = self.value()
        path = preview.record_path("cleanup", value)
        record = {"status": "cleaning", "phase": "application", "preview": value,
                  "cleanupStartedAt": preview.timestamp(AT)}
        lifecycle = self.lifecycle(MemoryStore({path: record}))
        lifecycle.argo.value = application_for(value)
        with patch.object(preview, "now", return_value=AT + dt.timedelta(minutes=16)), \
                patch.object(lifecycle, "reap_unregistered_images"):
            with self.assertRaisesRegex(preview.Failure, "overdue application deletion"):
                lifecycle.reap()
        self.assertIn(path, lifecycle.store.data)
        self.assertEqual(lifecycle.argo.calls, [value["id"]])

    def test_shared_image_prevents_deletion(self):
        value = dict(self.value(), imageDigest="sha256:" + "d" * 64, registryRepositoryId="55")
        other = dict(value, id="other-123-555", sourcePipelineId="555")
        lifecycle = self.lifecycle(MemoryStore({"previews/other/123-555.yaml": {"status": "active", "preview": other}}))
        done, skipped = lifecycle.delete_tags(value)
        self.assertFalse(done)
        self.assertTrue(skipped)
        self.assertFalse(lifecycle.api.calls)

    def test_redirect_strips_cross_origin_tokens(self):
        request = urllib.request.Request("https://gitlab.company.example/api/v4/jobs/1/artifacts", headers={"PRIVATE-TOKEN": "secret", "JOB-TOKEN": "job-secret"})
        redirected = preview.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://storage.example/artifact?signature=value")
        self.assertFalse(any(key.lower() in {"private-token", "job-token"} for key in redirected.headers))
        with self.assertRaises(preview.Failure):
            preview.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://storage.example/artifact")

    def test_disabled_or_unsupported_catalog_rejected(self):
        with self.assertRaises(preview.Failure):
            preview.validate_catalog(dict(CATALOG, enabled=False), SOURCE)
        with self.assertRaises(preview.Failure):
            preview.validate_catalog(dict(CATALOG, service="payment"), SOURCE)

    def test_catalog_requires_the_namespace_chart_in_the_helm_repository(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["provisioning"].update(
            repository="https://gitlab.company.example/company/previews.git", path="charts/previews")
        self.assertEqual(preview.validate_catalog(catalog, SOURCE), catalog)
        for repository, path in (
            ("https://gitlab.company.example/infrastructure/kubernetes.git", "charts/preview-namespace"),
            ("https://gitlab.company.example/company/previews.git", "example-app"),
        ):
            catalog["provisioning"].update(repository=repository, path=path)
            with self.subTest(repository=repository, path=path), self.assertRaises(preview.Failure):
                preview.validate_catalog(catalog, SOURCE)

    def test_legacy_project_cannot_acknowledge_a_previews_deployment(self):
        value = self.value()
        application = application_for(value)
        self.lifecycle().verify_application(application, value)
        application["spec"]["project"] = "preview"
        with self.assertRaisesRegex(preview.Failure, "project mismatch"):
            self.lifecycle().verify_application(application, value)

    def test_activation_requires_observed_registry_gate_health(self):
        value = self.value()
        application = application_for(value)
        lifecycle = self.lifecycle()
        self.assertTrue(lifecycle.application_ready(application, value))
        for state in ("Progressing", None):
            candidate = copy.deepcopy(application)
            candidate["status"]["resources"][0]["health"] = {} if state is None else {"status": state}
            with self.subTest(state=state):
                self.assertFalse(lifecycle.application_ready(candidate, value))
        application["status"]["resources"][0]["namespace"] = "production"
        self.assertFalse(lifecycle.application_ready(application, value))

    def test_registry_secret_delivery_cannot_be_omitted_from_provisioning(self):
        value = preview.make_preview(SOURCE, CATALOG, AT, "b" * 40)
        catalog = copy.deepcopy(CATALOG)
        catalog["provisioning"].pop("registrySecretDelivery")
        with self.assertRaisesRegex(preview.Failure, "registry-secret delivery"):
            preview.provisioning_values(value, catalog)

    def test_real_bridge_response_without_project_id_is_accepted(self):
        class API:
            def request(self, method, path, **kwargs):
                if "/pipelines/" in path:
                    return {"sha": SOURCE["sourceSha"], "ref": SOURCE["sourceTag"]}
                return {"commit": {"id": SOURCE["sourceSha"]}}

            def pages(self, path):
                return [{"downstream_pipeline": {"id": 999, "sha": "b" * 40, "ref": "main", "status": "running", "web_url": "https://gitlab.company.example/company/previews/-/pipelines/999"}}]
        lifecycle = preview.Lifecycle(API(), MemoryStore(), FakeArgo(), "777", "999")
        self.assertEqual(lifecycle.verified_source(ENV), SOURCE)

    def test_provisioning_resources_loaded_from_pinned_catalog_commit(self):
        value = self.value()
        catalog = copy.deepcopy(CATALOG)
        catalog["provisioning"]["networkPolicyFiles"] = ["provisioning/example-app/network.json"]
        resource = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "approved-traffic"}, "spec": {"podSelector": {}}}
        calls = []
        class API:
            def request(self, method, path, **kwargs):
                calls.append(path)
                return {"content": base64.b64encode(json.dumps(catalog if "catalog%2F" in path else resource).encode()).decode()}
        lifecycle = preview.Lifecycle(API(), MemoryStore(), FakeArgo(), "777", "999")
        actual = lifecycle.pinned_catalog(value)
        self.assertEqual(actual["provisioning"]["resolvedResources"], [resource])
        self.assertTrue(all(path.endswith("?ref=" + value["catalogRevision"]) for path in calls))

    def test_cleanup_refuses_application_without_foreground_finalizer(self):
        value = self.value()
        watched = preview.record_path("previews", value)
        lifecycle = self.lifecycle(MemoryStore({watched: {"status": "active", "preview": value}}))
        path, record = lifecycle.begin_cleanup(watched, lifecycle.store.read(watched), "manual stop")
        lifecycle.argo.value = application_for(value)
        lifecycle.argo.value["metadata"]["finalizers"] = []
        with self.assertRaises(preview.Failure):
            lifecycle.advance_cleanup(path, record)
        self.assertIn(watched, lifecycle.store.data)

    def test_final_cleanup_marks_all_alias_requests_stopped(self):
        value = self.value()
        path = preview.record_path("cleanup", value)
        record = {"status": "cleaning", "phase": "environment", "preview": value}
        store = MemoryStore({path: record, "requests/123-45678.json": {"status": "active", "preview": value}, "requests/123-88888.json": {"status": "active", "preview": value}})
        lifecycle = self.lifecycle(store)
        self.assertTrue(lifecycle.advance_cleanup(path, record))
        self.assertNotIn(path, store.data)
        for key in ("requests/123-45678.json", "requests/123-88888.json"):
            self.assertEqual(store.data[key]["status"], "stopped")
            self.assertFalse(store.data[key]["buildRequired"])


if __name__ == "__main__":
    unittest.main()
