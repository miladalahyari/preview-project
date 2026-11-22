import copy
import datetime as dt
import json
import unittest
from unittest.mock import Mock, patch

from test_preview import AT, CATALOG, ENV, SOURCE, FakeAPI, FakeArgo, MemoryStore, application_for, preview


class EnvironmentAPI(FakeAPI):
    def pages(self, path):
        if "/environments?" in path:
            return []
        raise AssertionError(path)

    def request(self, method, path, data=None, **kwargs):
        if method == "POST" and path == "/projects/777/environments":
            self.calls.append((method, path, data))
            return {"id": 51, "name": data["name"]}
        return super().request(method, path, data, **kwargs)


class GitOpsLifecycleTests(unittest.TestCase):
    def setup_pending(self):
        value = preview.make_preview(SOURCE, CATALOG, AT, "b" * 40)
        pending_path = preview.record_path("pending", value)
        request = {"status": "pending", "buildRequired": True, "recordPath": pending_path, "preview": value}
        store = MemoryStore({pending_path: {"status": "pending", "preview": value}, preview.request_path(SOURCE): request})
        lifecycle = preview.Lifecycle(EnvironmentAPI(), store, FakeArgo(), "777", "999")
        lifecycle.verified_source = lambda env, **kwargs: dict(SOURCE)
        lifecycle.pinned_catalog = lambda value: copy.deepcopy(CATALOG)
        lifecycle.build_metadata = Mock(return_value=("sha256:" + "d" * 64, "55"))
        return lifecycle, value

    def register(self):
        lifecycle, value = self.setup_pending()
        with patch.object(preview, "now", return_value=AT):
            request = lifecycle.register(ENV)
        lifecycle.argo.value = application_for(request["preview"])
        return lifecycle, request

    def test_register_publishes_provisioning_without_starting_ttl_or_mutating_cluster(self):
        lifecycle, original = self.setup_pending()
        with patch.object(preview, "now", return_value=AT):
            request = lifecycle.register(ENV)
        self.assertEqual(request["status"], "provisioning")
        self.assertFalse(request["buildRequired"])
        self.assertEqual(request["preview"]["expiresAt"], "")
        self.assertEqual(request["preview"]["bootstrapRevision"], "c" * 40)
        self.assertNotIn(preview.record_path("pending", original), lifecycle.store.data)
        self.assertEqual(lifecycle.store.data[request["recordPath"]]["status"], "provisioning")
        self.assertEqual(request["preview"]["provisioning"]["resources"][0]["metadata"]["namespace"], original["namespace"])
        self.assertFalse(lifecycle.argo.calls)
        self.assertFalse(any(method == "POST" for method, _, _ in lifecycle.api.calls))

    def test_register_retry_reuses_verified_provisioning_state_and_deadline(self):
        lifecycle, first = self.register()
        lifecycle.build_metadata.reset_mock()
        with patch.object(preview, "now", return_value=AT + dt.timedelta(minutes=4)):
            again = lifecycle.register(ENV)
        self.assertEqual(again["preview"], first["preview"])
        self.assertEqual(again["status"], "provisioning")
        lifecycle.build_metadata.assert_not_called()

    def test_prepare_rejects_in_flight_provisioning_without_mutating_state(self):
        lifecycle, _ = self.register()
        before = len(lifecycle.store.writes)
        with patch.object(preview, "choose_catalog", return_value=CATALOG), patch.object(preview, "now", return_value=AT):
            with self.assertRaisesRegex(preview.Failure, "provisioning"):
                lifecycle.prepare(ENV)
        self.assertEqual(len(lifecycle.store.writes), before)

    def test_ready_observes_both_sources_and_does_not_start_lease(self):
        lifecycle, request = self.register()
        before = len(lifecycle.store.writes)
        with patch.object(preview, "now", return_value=AT + dt.timedelta(minutes=2)):
            result = lifecycle.ready(ENV)
        self.assertEqual(result["status"], "provisioning")
        self.assertEqual(result["preview"]["expiresAt"], "")
        self.assertEqual(len(lifecycle.store.writes), before)
        self.assertEqual(lifecycle.argo.calls, [request["preview"]["id"]])

    def test_readiness_timeout_leaves_durable_provisioning_for_reaper(self):
        lifecycle, request = self.register()
        lifecycle.argo.value = None
        with patch.object(preview, "now", return_value=AT), patch.object(preview.time, "sleep"), patch.object(preview.time, "monotonic", side_effect=[0, 0, 601]):
            with self.assertRaisesRegex(preview.Failure, "readiness timeout"):
                lifecycle.ready(ENV)
        self.assertEqual(lifecycle.store.data[request["recordPath"]]["status"], "provisioning")
        self.assertEqual(lifecycle.store.data[request["recordPath"]]["preview"]["expiresAt"], "")

    def test_activation_starts_full_lease_after_readiness_and_retry_preserves_it(self):
        lifecycle, request = self.register()
        activated_at = AT + dt.timedelta(minutes=4)
        with patch.object(preview, "now", return_value=activated_at):
            active = lifecycle.activate(ENV)
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["preview"]["activatedAt"], preview.timestamp(activated_at))
        self.assertEqual(active["preview"]["expiresAt"], preview.timestamp(activated_at + dt.timedelta(hours=12)))
        self.assertEqual(active["preview"]["environmentId"], "51")
        lifecycle.argo.value = application_for(active["preview"])
        before = len(lifecycle.store.writes)
        with patch.object(preview, "now", return_value=AT + dt.timedelta(hours=1)):
            retried = lifecycle.activate(ENV)
        self.assertEqual(retried["preview"]["expiresAt"], active["preview"]["expiresAt"])
        self.assertEqual(len(lifecycle.store.writes), before)

    def test_activation_fails_after_setup_expiry_stop_or_health_regression(self):
        for state in ("expired", "stopped", "unhealthy"):
            with self.subTest(state=state):
                lifecycle, request = self.register()
                if state == "stopped":
                    lifecycle.begin_cleanup(request["recordPath"], lifecycle.store.read(request["recordPath"]), "manual stop")
                elif state == "unhealthy":
                    lifecycle.argo.value["status"]["health"]["status"] = "Degraded"
                instant = AT + dt.timedelta(minutes=31 if state == "expired" else 1)
                with patch.object(preview, "now", return_value=instant):
                    with self.assertRaises(preview.Failure):
                        lifecycle.activate(ENV)
                self.assertFalse(any(method == "POST" for method, _, _ in lifecycle.api.calls))

    def test_foreign_bootstrap_namespace_and_additional_overrides_are_rejected(self):
        lifecycle, request = self.register()
        value = request["preview"]
        for alteration in ("namespace", "source", "helm", "image", "values-file"):
            with self.subTest(alteration=alteration):
                application = application_for(value)
                if alteration == "namespace":
                    bootstrap = application["spec"]["sources"][0]["helm"]
                    values = json.loads(bootstrap["values"])
                    values["preview"]["namespace"] = "production"
                    bootstrap["values"] = json.dumps(values)
                elif alteration == "source":
                    application["spec"]["sources"][0]["plugin"] = {"name": "unapproved"}
                elif alteration == "helm":
                    application["spec"]["sources"][0]["helm"]["valuesObject"] = {"preview": {"namespace": "production"}}
                elif alteration == "image":
                    application["spec"]["sources"][1]["helm"]["parameters"][1]["value"] = "sha256:" + "e" * 64
                else:
                    application["spec"]["sources"][1]["helm"]["valueFiles"] = ["values.yaml"]
                with self.assertRaises(preview.Failure):
                    lifecycle.verify_application(application, value)

    def test_stale_sync_revisions_or_compared_sources_cannot_activate(self):
        lifecycle, request = self.register()
        value = request["preview"]
        for field in ("revisions", "comparedTo"):
            application = application_for(value)
            if field == "revisions":
                application["status"]["sync"][field][0] = "e" * 40
            else:
                application["status"]["sync"][field]["sources"] = []
            self.assertFalse(lifecycle.application_ready(application, value))

    def test_published_cleanup_observes_then_removes_record_before_waiting_for_finalizer(self):
        lifecycle, request = self.register()
        path, record = lifecycle.begin_cleanup(request["recordPath"], lifecycle.store.read(request["recordPath"]), "manual stop")
        self.assertEqual(record["phase"], "observe")
        self.assertIn(request["recordPath"], lifecycle.store.data)
        lifecycle.advance_cleanup(path, record)
        self.assertNotIn(request["recordPath"], lifecycle.store.data)
        self.assertEqual(record["phase"], "application")
        self.assertFalse(lifecycle.advance_cleanup(path, record))
        lifecycle.argo.value = None
        self.assertFalse(lifecycle.advance_cleanup(path, record))
        self.assertEqual(record["phase"], "tags")

    def test_unpublished_pending_cleanup_never_looks_up_an_application(self):
        lifecycle, value = self.setup_pending()
        pending = preview.record_path("pending", value)
        path, record = lifecycle.begin_cleanup(pending, lifecycle.store.read(pending), "setup timeout")
        self.assertEqual(record["phase"], "tags")
        self.assertNotIn(pending, lifecycle.store.data)
        self.assertFalse(lifecycle.argo.calls)

    def test_provisioning_timeout_uses_setup_deadline_and_holds_watched_cleanup(self):
        lifecycle, request = self.register()
        lifecycle.argo.value = None
        lifecycle.reap_unregistered_images = lambda: None
        with patch.object(preview, "now", return_value=AT + dt.timedelta(minutes=31)):
            lifecycle.reap()
        cleanup = lifecycle.store.read(preview.record_path("cleanup", request["preview"]))
        self.assertEqual(cleanup["reason"], "setup timeout")
        self.assertEqual(cleanup["phase"], "observe")
        self.assertEqual(lifecycle.store.data[request["recordPath"]]["status"], "cleaning")
        self.assertEqual(lifecycle.store.read(preview.request_path(SOURCE))["status"], "cleaning")
