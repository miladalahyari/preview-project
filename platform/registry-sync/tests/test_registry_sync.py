import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request


SPEC = importlib.util.spec_from_file_location("registry_sync", Path(__file__).resolve().parents[1] / "registry_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def source(version="1", password="private-registry-password", host="https://cr.company.example"):
    data = {"auths": {host: {"auth": base64.b64encode(("registry-user:" + password).encode()).decode()}}}
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": sync.SECRET_NAME, "namespace": "previews", "resourceVersion": version,
            "uid": "do-not-copy-source-uid", "labels": {"app.kubernetes.io/instance": "source-argo-app"}, "annotations": {"private": "source annotation"},
            "ownerReferences": [{"uid": "do-not-copy-source-owner"}], "finalizers": ["do-not-copy-source-finalizer"]},
            "type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": base64.b64encode(json.dumps(data).encode()).decode()}}


def namespace(project="123", pipeline="45678", **overrides):
    preview = "example-app-" + project + "-" + pipeline
    value = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "test-" + preview, "uid": "namespace-" + pipeline,
             "labels": {sync.PREFIX + "preview-id": preview, sync.PREFIX + "source-project-id": project, sync.PREFIX + "ephemeral": "true"}}}
    value["metadata"].update(overrides)
    return value


def gate(ns):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": sync.GATE_NAME, "namespace": ns["metadata"]["name"],
             "resourceVersion": "1", "labels": dict(ns["metadata"]["labels"], **{sync.PREFIX + "registry-gate": "true"}),
             "annotations": {"argocd.argoproj.io/sync-wave": "-10", "argocd.argoproj.io/tracking-id": "keep-this-tracking-metadata"}},
            "data": {"state": "Pending", "sourceResourceVersion": ""}}


class FakeKubernetes:
    def __init__(self, namespaces=None):
        self.source = source()
        self.namespaces = copy.deepcopy(namespaces if namespaces is not None else [namespace()])
        self.current_namespaces = {ns["metadata"]["name"]: copy.deepcopy(ns) for ns in self.namespaces}
        self.gates = {ns["metadata"]["name"]: gate(ns) for ns in self.namespaces}
        self.secrets = {}
        self.calls = []
        self.failures = {}
        self.pages = None
        self.next_version = 2

    def fail(self, method, path, errors):
        self.failures[(method, path)] = list(errors)

    def request(self, method, path, body=None, absent=False):
        self.calls.append((method, path, copy.deepcopy(body)))
        failures = self.failures.get((method, path), [])
        if failures:
            error = failures.pop(0)
            if error:
                raise error
        source_path = sync.resource_path("previews", "secrets", sync.SECRET_NAME)
        if path == source_path:
            assert method == "GET"
            if self.source is None:
                raise sync.APIError(404)
            return copy.deepcopy(self.source)
        if path.startswith("/api/v1/namespaces?"):
            assert method == "GET"
            if self.pages is not None:
                return copy.deepcopy(self.pages.pop(0))
            return {"items": copy.deepcopy(self.namespaces), "metadata": {}}
        parts = path.split("/")
        assert parts[:4] == ["", "api", "v1", "namespaces"], path
        name = parts[4]
        if len(parts) == 5:
            assert method == "GET"
            return copy.deepcopy(self.current_namespaces.get(name))
        kind = parts[5]
        table = self.secrets if kind == "secrets" else self.gates
        if method == "GET":
            return copy.deepcopy(table.get(name))
        if method == "POST":
            assert kind == "secrets" and len(parts) == 6
            if name in table:
                raise sync.APIError(409)
            table[name] = copy.deepcopy(body)
            table[name]["metadata"]["resourceVersion"] = str(self.next_version)
            self.next_version += 1
            return copy.deepcopy(table[name])
        assert method == "PATCH"
        current = table[name]
        if body["metadata"]["resourceVersion"] != current["metadata"]["resourceVersion"]:
            raise sync.APIError(409)
        for key, value in body["data"].items():
            if value is None:
                current.setdefault("data", {}).pop(key, None)
            else:
                current.setdefault("data", {})[key] = value
        current["metadata"]["resourceVersion"] = str(self.next_version)
        self.next_version += 1
        return copy.deepcopy(current)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.health = Path(self.directory.name) / "health.json"
        self.api = FakeKubernetes()
        self.events = []
        self.controller = sync.Controller(self.api, "123", self.health, self.events.append)
        self.ns = self.api.namespaces[0]["metadata"]["name"]

    def run_healthy(self):
        self.assertTrue(self.controller.run_once())
        return self.api.secrets[self.ns]

    def test_copies_secret_before_acknowledging_gate_and_never_copies_source_metadata(self):
        target = self.run_healthy()
        self.assertEqual(target["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(target["data"], self.api.source["data"])
        metadata = target["metadata"]
        self.assertEqual(metadata["namespace"], self.ns)
        self.assertEqual(metadata["labels"][sync.PREFIX + "managed-by"], sync.MANAGER)
        for key in ("annotations", "ownerReferences", "finalizers", "uid"):
            self.assertNotIn(key, metadata)
        gate = self.api.gates[self.ns]
        self.assertEqual(gate["data"], {"state": "Ready", "sourceResourceVersion": "1"})
        writes = [(method, path) for method, path, _ in self.api.calls if method != "GET"]
        self.assertEqual(writes, [("POST", sync.resource_path(self.ns, "secrets")), ("PATCH", sync.resource_path(self.ns, "configmaps", sync.GATE_NAME))])

    def test_retries_are_idempotent_and_do_not_write_unchanged_secrets_or_gates(self):
        first = copy.deepcopy(self.run_healthy())
        self.api.calls.clear()
        self.assertTrue(self.controller.run_once())
        self.assertEqual(self.api.secrets[self.ns], first)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_rotation_updates_data_then_acknowledges_new_public_source_version(self):
        self.run_healthy()
        self.api.source = source("2", "rotated-private-registry-password")
        self.api.calls.clear()
        self.assertTrue(self.controller.run_once())
        self.assertEqual(self.api.secrets[self.ns]["data"], self.api.source["data"])
        self.assertEqual(self.api.gates[self.ns]["data"], {"state": "Ready", "sourceResourceVersion": "2"})
        writes = [path for method, path, _ in self.api.calls if method == "PATCH"]
        self.assertEqual(writes, [sync.resource_path(self.ns, "secrets", sync.SECRET_NAME), sync.resource_path(self.ns, "configmaps", sync.GATE_NAME)])

    def test_source_version_change_with_same_data_needs_only_gate_patch(self):
        self.run_healthy()
        self.api.source["metadata"]["resourceVersion"] = "9"
        self.api.calls.clear()
        self.assertTrue(self.controller.run_once())
        writes = [path for method, path, _ in self.api.calls if method != "GET"]
        self.assertEqual(writes, [sync.resource_path(self.ns, "configmaps", sync.GATE_NAME)])

    def test_source_errors_make_an_existing_ready_gate_pending_without_changing_secret(self):
        target = copy.deepcopy(self.run_healthy())
        path = sync.resource_path("previews", "secrets", sync.SECRET_NAME)
        for status in (401, 403, 404, None):
            with self.subTest(status=status):
                self.api.gates[self.ns]["data"] = {"state": "Ready", "sourceResourceVersion": "1"}
                self.api.fail("GET", path, [sync.APIError(status)])
                self.assertFalse(self.controller.run_once())
                self.assertEqual(self.api.gates[self.ns]["data"], {"state": "Pending", "sourceResourceVersion": ""})
                self.assertEqual(self.api.secrets[self.ns], target)
                self.assertFalse(json.loads(self.health.read_text())["sourceReady"])

    def test_invalid_source_registry_never_copies_and_logs_no_private_value(self):
        self.api.source = source(host="attacker.example", password="unique-sensitive-value")
        self.assertFalse(self.controller.run_once())
        self.assertFalse(self.api.secrets)
        self.assertEqual(self.api.gates[self.ns]["data"]["state"], "Pending")
        logs = json.dumps(self.events) + self.health.read_text()
        for private in ("unique-sensitive-value", self.api.source["data"][".dockerconfigjson"], "attacker.example"):
            self.assertNotIn(private, logs)

    def test_foreign_unowned_mismatched_and_terminating_namespaces_are_untouched(self):
        examples = [namespace("124"), namespace(labels={}), namespace(deletionTimestamp="2026-11-10T00:00:00Z"),
                    namespace(name="production")]
        for ns in examples:
            with self.subTest(name=ns["metadata"]["name"]):
                api = FakeKubernetes([ns])
                controller = sync.Controller(api, "123", self.health, self.events.append)
                self.assertTrue(controller.run_once())
                self.assertFalse(api.secrets)
                self.assertTrue(all(method == "GET" for method, _, _ in api.calls))

    def test_namespace_deletion_or_uid_replacement_after_list_is_skipped(self):
        for change in ("deletion", "replacement", "missing"):
            with self.subTest(change=change):
                api = FakeKubernetes()
                if change == "deletion": api.current_namespaces[self.ns]["metadata"]["deletionTimestamp"] = "now"
                elif change == "replacement": api.current_namespaces[self.ns]["metadata"]["uid"] = "different-uid"
                else: api.current_namespaces.pop(self.ns)
                controller = sync.Controller(api, "123", self.health, self.events.append)
                self.assertTrue(controller.run_once())
                self.assertFalse(api.secrets)

    def test_unowned_existing_secret_is_never_adopted_and_gate_is_pending(self):
        self.run_healthy()
        self.api.secrets[self.ns]["metadata"]["labels"].pop(sync.PREFIX + "managed-by")
        original = copy.deepcopy(self.api.secrets[self.ns])
        self.api.calls.clear()
        self.assertFalse(self.controller.run_once())
        self.assertEqual(self.api.secrets[self.ns], original)
        self.assertEqual(self.api.gates[self.ns]["data"]["state"], "Pending")
        self.assertFalse(any(method in ("POST", "PATCH") and "/secrets" in path for method, path, _ in self.api.calls))

    def test_foreign_tracked_immutable_wrong_type_or_extra_data_secret_is_rejected(self):
        self.run_healthy()
        original = copy.deepcopy(self.api.secrets[self.ns])
        variants = []
        foreign = copy.deepcopy(original);foreign["metadata"]["labels"][sync.PREFIX + "source-project-id"] = "999";variants.append(foreign)
        tracked = copy.deepcopy(original);tracked["metadata"]["annotations"] = {"argocd.argoproj.io/tracking-id": "foreign"};variants.append(tracked)
        updater = copy.deepcopy(original);updater["metadata"]["annotations"] = {"argocd-image-updater.argoproj.io/image-list": "foreign"};variants.append(updater)
        owner = copy.deepcopy(original);owner["metadata"]["ownerReferences"] = [{"uid": "foreign"}];variants.append(owner)
        immutable = copy.deepcopy(original);immutable["immutable"] = True;variants.append(immutable)
        wrong_type = copy.deepcopy(original);wrong_type["type"] = "Opaque";variants.append(wrong_type)
        extra = copy.deepcopy(original);extra["data"]["foreign"] = "value";variants.append(extra)
        extra_label = copy.deepcopy(original);extra_label["metadata"]["labels"]["foreign"] = "value";variants.append(extra_label)
        for candidate in variants:
            with self.subTest(candidateKeys=list(candidate)):
                self.api.secrets[self.ns] = copy.deepcopy(candidate)
                self.assertFalse(self.controller.run_once())
                self.assertEqual(self.api.secrets[self.ns], candidate)
                self.assertEqual(self.api.gates[self.ns]["data"]["state"], "Pending")

    def test_unowned_gate_is_never_adopted_or_used_to_trigger_a_secret_copy(self):
        self.api.gates[self.ns]["metadata"]["labels"].pop(sync.PREFIX + "registry-gate")
        original = copy.deepcopy(self.api.gates[self.ns])
        self.assertFalse(self.controller.run_once())
        self.assertFalse(self.api.secrets)
        self.assertEqual(self.api.gates[self.ns], original)

    def test_gate_extra_data_labels_foreign_tracking_or_ownership_fail_closed(self):
        original = copy.deepcopy(self.api.gates[self.ns])
        variants = []
        extra_data = copy.deepcopy(original);extra_data["data"]["foreign"] = "value";variants.append(extra_data)
        binary_data = copy.deepcopy(original);binary_data["binaryData"] = {"foreign": "dmFsdWU="};variants.append(binary_data)
        extra_label = copy.deepcopy(original);extra_label["metadata"]["labels"]["foreign"] = "value";variants.append(extra_label)
        foreign_tracking = copy.deepcopy(original);foreign_tracking["metadata"]["labels"]["app.kubernetes.io/instance"] = "foreign";variants.append(foreign_tracking)
        owner = copy.deepcopy(original);owner["metadata"]["ownerReferences"] = [{"uid": "foreign"}];variants.append(owner)
        immutable = copy.deepcopy(original);immutable["immutable"] = True;variants.append(immutable)
        no_version = copy.deepcopy(original);no_version["data"] = {"state": "Ready", "sourceResourceVersion": ""};variants.append(no_version)
        for candidate in variants:
            with self.subTest(candidateKeys=list(candidate)):
                self.api.gates[self.ns] = copy.deepcopy(candidate)
                self.api.calls.clear()
                self.assertFalse(self.controller.run_once())
                self.assertEqual(self.api.gates[self.ns], candidate)
                self.assertFalse(any(method != "GET" for method, _, _ in self.api.calls))

    def test_gate_expected_argo_labels_are_preserved(self):
        self.api.gates[self.ns]["metadata"]["labels"].update({"app.kubernetes.io/part-of": "preview-platform", "app.kubernetes.io/instance": "example-app-123-45678"})
        labels = copy.deepcopy(self.api.gates[self.ns]["metadata"]["labels"])
        self.run_healthy()
        self.assertEqual(self.api.gates[self.ns]["metadata"]["labels"], labels)

    def test_missing_gate_waits_without_creating_configmap_or_secret(self):
        self.api.gates.pop(self.ns)
        self.assertTrue(self.controller.run_once())
        self.assertFalse(self.api.secrets)
        self.assertEqual(json.loads(self.health.read_text())["waitingNamespaces"], 1)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_target_authorization_or_transport_failure_never_means_missing_or_ready(self):
        path = sync.resource_path(self.ns, "secrets", sync.SECRET_NAME)
        for status in (401, 403, None):
            with self.subTest(status=status):
                api = FakeKubernetes()
                api.fail("GET", path, [sync.APIError(status)])
                controller = sync.Controller(api, "123", self.health, self.events.append)
                self.assertFalse(controller.run_once())
                self.assertFalse(api.secrets)
                self.assertEqual(api.gates[self.ns]["data"]["state"], "Pending")

    def test_secret_create_conflict_is_retried(self):
        path = sync.resource_path(self.ns, "secrets")
        self.api.fail("POST", path, [sync.APIError(409)])
        self.run_healthy()
        self.assertEqual(sum(method == "POST" for method, _, _ in self.api.calls), 2)

    def test_secret_update_conflict_is_retried(self):
        self.run_healthy()
        self.api.source = source("2", "rotated")
        self.api.calls.clear()
        path = sync.resource_path(self.ns, "secrets", sync.SECRET_NAME)
        self.api.fail("PATCH", path, [sync.APIError(409)])
        self.assertTrue(self.controller.run_once())
        self.assertEqual(sum(method == "PATCH" and target == path for method, target, _ in self.api.calls), 2)

    def test_gate_conflict_is_retried_and_argo_tracking_metadata_is_preserved(self):
        original_metadata = copy.deepcopy(self.api.gates[self.ns]["metadata"])
        path = sync.resource_path(self.ns, "configmaps", sync.GATE_NAME)
        self.api.fail("PATCH", path, [sync.APIError(409)])
        self.run_healthy()
        metadata = copy.deepcopy(self.api.gates[self.ns]["metadata"])
        metadata.pop("resourceVersion");original_metadata.pop("resourceVersion")
        self.assertEqual(metadata, original_metadata)
        self.assertEqual(sum(method == "PATCH" and target == path for method, target, _ in self.api.calls), 2)

    def test_repeated_conflicts_leave_gate_pending_and_report_error(self):
        path = sync.resource_path(self.ns, "secrets")
        self.api.fail("POST", path, [sync.APIError(409)] * 3)
        self.assertFalse(self.controller.run_once())
        self.assertEqual(self.api.gates[self.ns]["data"]["state"], "Pending")
        self.assertEqual(self.events[-1]["httpStatus"], 409)

    def test_gate_ready_write_failure_is_visible_and_best_effort_reset_is_pending(self):
        path = sync.resource_path(self.ns, "configmaps", sync.GATE_NAME)
        self.api.fail("PATCH", path, [sync.APIError(403)])
        self.assertFalse(self.controller.run_once())
        self.assertIn(self.ns, self.api.secrets)
        self.assertEqual(self.api.gates[self.ns]["data"]["state"], "Pending")

    def test_namespace_pagination_is_followed_and_repeated_continuation_is_rejected(self):
        other = namespace(pipeline="45679")
        self.api.namespaces.append(other)
        self.api.current_namespaces[other["metadata"]["name"]] = copy.deepcopy(other)
        self.api.gates[other["metadata"]["name"]] = gate(other)
        self.api.pages = [{"items": [self.api.namespaces[0]], "metadata": {"continue": "next"}}, {"items": [other], "metadata": {}}]
        self.assertTrue(self.controller.run_once())
        self.assertEqual(len(self.api.secrets), 2)
        self.assertTrue(any("continue=next" in path for _, path, _ in self.api.calls))
        self.api.pages = [{"items": [], "metadata": {"continue": "same"}}] * 2
        self.assertFalse(self.controller.run_once())

    def test_health_contains_only_public_operational_fields(self):
        self.run_healthy()
        health = json.loads(self.health.read_text())
        self.assertEqual(set(health), {"lastAttempt", "lastSuccess", "sourceReady", "eligibleNamespaces", "errors", "waitingNamespaces"})
        self.assertTrue(health["sourceReady"])
        self.assertGreater(health["lastAttempt"], 0)
        self.assertGreater(health["lastSuccess"], 0)
        self.assertEqual(health["eligibleNamespaces"], 1)
        self.assertEqual(health["errors"], 0)
        self.assertNotIn("private-registry-password", self.health.read_text())

    def test_health_symlink_is_not_followed(self):
        outside = Path(self.directory.name) / "untouched"
        outside.write_text("untouched")
        self.health.symlink_to(outside)
        with self.assertRaises(OSError):
            self.controller.run_once()
        self.assertEqual(outside.read_text(), "untouched")

    def test_health_update_keeps_last_complete_snapshot_visible_until_replacement(self):
        self.run_healthy()
        previous = json.loads(self.health.read_text())
        self.controller.health["errors"] = 7
        observations = []

        def write_in_parts(value, stream, **kwargs):
            encoded = json.dumps(value, **kwargs)
            split = len(encoded) // 2
            stream.write(encoded[:split])
            stream.flush()
            observations.append(json.loads(self.health.read_text()))
            stream.write(encoded[split:])

        with patch.object(sync.json, "dump", side_effect=write_in_parts):
            self.controller.heartbeat()
        self.assertEqual(observations, [previous])
        self.assertEqual(json.loads(self.health.read_text())["errors"], 7)
        self.assertEqual(self.health.stat().st_mode & 0o777, 0o644)

    def test_health_temporary_symlink_is_not_followed(self):
        self.run_healthy()
        previous = self.health.read_bytes()
        outside = Path(self.directory.name) / "untouched"
        outside.write_text("untouched")
        temporary = self.health.with_name("." + self.health.name + ".tmp")
        temporary.symlink_to(outside)
        with self.assertRaises(OSError):
            self.controller.heartbeat()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertEqual(self.health.read_bytes(), previous)

    def test_invalid_project_configuration_fails_closed(self):
        for project in ("", "0", "0123", "../production", "123\n", None):
            with self.subTest(project=project), self.assertRaises(sync.ValidationError):
                sync.Controller(self.api, project, self.health)


class SourceValidationTests(unittest.TestCase):
    def test_only_the_exact_registry_host_is_accepted(self):
        for host in ("cr.company.example", "https://cr.company.example", "https://cr.company.example/", "https://cr.company.example:443"):
            with self.subTest(host=host):
                self.assertEqual(sync.validate_source(source(host=host))["version"], "1")
        for host in ("http://cr.company.example", "https://cr.company.example.attacker.example", "https://user:password@cr.company.example", "https://cr.company.example:444", "https://cr.company.example/path", "https://cr.company.example?x=y", " cr.company.example"):
            with self.subTest(host=host), self.assertRaises(sync.ValidationError):
                sync.validate_source(source(host=host))

    def test_wrong_type_identity_version_or_malformed_data_is_rejected(self):
        variants = []
        for key, value in (("name", "foreign"), ("namespace", "production"), ("resourceVersion", "")):
            candidate = source();candidate["metadata"][key] = value;variants.append(candidate)
        candidate = source();candidate["type"] = "Opaque";variants.append(candidate)
        candidate = source();candidate["data"] = {};variants.append(candidate)
        for value in ("!not-base64", base64.b64encode(b"not-json").decode(), base64.b64encode(b'{"auths":{}}').decode()):
            candidate = source();candidate["data"][".dockerconfigjson"] = value;variants.append(candidate)
        for candidate in variants:
            with self.subTest(candidateType=candidate.get("type")), self.assertRaises(sync.ValidationError):
                sync.validate_source(candidate)


class KubernetesClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_path = Path(self.directory.name) / "token"
        self.token_path.write_text("projected-token-one")
        self.opener = Mock()
        self.client = sync.Kubernetes(self.token_path, opener=self.opener)

    def respond(self, value):
        self.opener.open.return_value = io.BytesIO(json.dumps(value).encode())

    def error(self, status):
        return urllib.error.HTTPError(sync.API_SERVER + "/api/v1/namespaces", status, "sensitive error", {}, io.BytesIO(b"sensitive registry credential body"))

    def test_projected_token_is_reread_and_requests_remain_on_fixed_https_origin(self):
        for token in ("projected-token-one", "projected-token-two"):
            self.token_path.write_text(token)
            self.respond({"kind": "Secret"})
            self.client.request("GET", "/api/v1/namespaces/previews/secrets/company-registry")
            request = self.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, sync.API_SERVER + "/api/v1/namespaces/previews/secrets/company-registry")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + token)
            self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 10)

    def test_merge_patch_sets_correct_content_type_and_resource_version(self):
        self.respond({})
        self.client.request("PATCH", "/api/v1/namespaces/test-example-app-123-45678/configmaps/preview-registry-ready", {"metadata": {"resourceVersion": "7"}, "data": {"state": "Ready"}})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.get_header("Content-type"), "application/merge-patch+json")
        self.assertEqual(json.loads(request.data)["metadata"]["resourceVersion"], "7")

    def test_only_get_404_is_missing_and_no_error_body_is_logged(self):
        self.opener.open.side_effect = self.error(404)
        self.assertIsNone(self.client.request("GET", "/api/v1/namespaces/a/secrets/b", absent=True))
        for status in (401, 403, 409, 500):
            self.opener.open.side_effect = self.error(status)
            with self.subTest(status=status), self.assertRaises(sync.APIError) as raised:
                self.client.request("GET", "/api/v1/namespaces/a/secrets/b", absent=True)
            self.assertEqual(raised.exception.status, status)
            self.assertNotIn("sensitive", str(raised.exception))
        self.opener.open.side_effect = self.error(404)
        with self.assertRaises(sync.APIError):
            self.client.request("POST", "/api/v1/namespaces/a/secrets", {}, absent=True)

    def test_network_timeouts_and_malformed_responses_are_generic_failures(self):
        for error in (urllib.error.URLError("sensitive private response"), TimeoutError("sensitive token")):
            self.opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(sync.APIError) as raised:
                self.client.request("GET", "/api/v1/namespaces")
            self.assertNotIn("sensitive", str(raised.exception))
        self.opener.open.side_effect = None
        self.opener.open.return_value = io.BytesIO(b"sensitive not JSON")
        with self.assertRaises(sync.APIError):
            self.client.request("GET", "/api/v1/namespaces")

    def test_redirects_unsafe_paths_and_header_injection_are_rejected(self):
        with self.assertRaises(sync.APIError):
            sync.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://attacker.example")
        for path in ("https://attacker.example", "//attacker.example", "/apis/rbac/v1/clusterroles", "/api/v1/namespaces#fragment"):
            with self.subTest(path=path), self.assertRaises(sync.ValidationError):
                self.client.request("GET", path)
        self.token_path.write_text("token\nInjected: private")
        with self.assertRaises(sync.ValidationError):
            self.client.request("GET", "/api/v1/namespaces")
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
