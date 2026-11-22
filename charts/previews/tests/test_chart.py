"""Render the actual Helm chart without contacting a cluster."""
import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]
EXAMPLE = yaml.safe_load((CHART / "examples/example-app.yaml").read_text())


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate key: " + str(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


@unittest.skipUnless(shutil.which("helm"), "helm is required for chart rendering")
class ChartTests(unittest.TestCase):
    def setUp(self):
        self.values = copy.deepcopy(EXAMPLE)

    def render(self, values=None):
        with tempfile.TemporaryDirectory(prefix="preview-chart-test-") as directory:
            path = Path(directory) / "values.yaml"
            path.write_text(yaml.safe_dump(self.values if values is None else values))
            return subprocess.run(
                ["helm", "template", "chart-test", str(CHART), "-f", str(path)],
                text=True, capture_output=True, check=False,
            )

    def objects(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        return [item for item in yaml.load_all(result.stdout, Loader=UniqueLoader) if item]

    def reject(self):
        result = self.render()
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_lint_example(self):
        result = subprocess.run(
            ["helm", "lint", str(CHART), "-f", str(CHART / "examples/example-app.yaml")],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_default_configuration_cannot_deploy(self):
        self.assertNotEqual(self.render({}).returncode, 0)

    def test_identity_labels_waves_and_isolation(self):
        objects = self.objects()
        self.assertEqual(len(objects), 7)
        self.assertEqual({o["kind"] for o in objects}, {"Namespace", "ResourceQuota", "LimitRange", "NetworkPolicy", "RoleBinding", "ConfigMap"})
        namespace = next(o for o in objects if o["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["name"], self.values["preview"]["namespace"])
        self.assertEqual(namespace["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-30")
        self.assertEqual(namespace["metadata"]["annotations"]["company.example/expires-at"], self.values["preview"]["expiresAt"])
        self.assertEqual(namespace["metadata"]["annotations"]["company.example/required-secrets"], '["company-registry"]')
        for item in objects:
            self.assertEqual(item["metadata"]["labels"]["company.example/preview-id"], self.values["preview"]["id"])
            self.assertEqual(item["metadata"]["labels"]["company.example/source-project-id"], "123")
            if item["kind"] != "Namespace":
                self.assertEqual(item["metadata"]["namespace"], self.values["preview"]["namespace"])
                self.assertEqual(item["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-10" if item["kind"] == "ConfigMap" else "-20")
        deny = next(o for o in objects if o["metadata"]["name"] == "default-deny")
        self.assertEqual(deny["spec"], {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
        quota = next(o for o in objects if o["kind"] == "ResourceQuota")
        self.assertEqual(quota["spec"]["hard"], self.values["provisioning"]["quota"])

    def test_separate_previews_have_disjoint_resource_identities(self):
        first = self.objects()
        self.values["preview"]["id"] = "example-app-123-45679"
        self.values["preview"]["namespace"] = "test-example-app-123-45679"
        second = self.objects()
        def ids(objects):
            return {(o["kind"], o["metadata"].get("namespace", ""), o["metadata"]["name"]) for o in objects}
        self.assertFalse(ids(first) & ids(second))

    def test_provisioning_empty_expiry_then_active_expiry(self):
        self.values["preview"]["expiresAt"] = ""
        namespace = next(o for o in self.objects() if o["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["annotations"]["company.example/expires-at"], "")
        self.values["preview"]["expiresAt"] = "2026-11-10T06:00:00Z"
        namespace = next(o for o in self.objects() if o["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["annotations"]["company.example/expires-at"], "2026-11-10T06:00:00Z")

    def test_registry_delivery_is_required_and_cannot_be_disabled(self):
        for value in [False, "true", None]:
            with self.subTest(value=value):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"]["registrySecretDelivery"] = value
                self.reject()
        self.values = copy.deepcopy(EXAMPLE)
        self.values["provisioning"].pop("registrySecretDelivery")
        self.reject()

    def test_fixed_registry_controller_gets_only_the_approved_role_binding(self):
        binding = next(o for o in self.objects() if o["kind"] == "RoleBinding")
        self.assertEqual(binding["metadata"]["name"], "preview-registry-sync")
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "preview-registry-target"})
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "apiGroup": "", "name": "preview-registry-sync", "namespace": "previews"}])
        self.assertEqual(binding["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-20")
        self.assertNotIn("rules", binding)

    def test_registry_gate_starts_pending_before_workload_wave(self):
        gate = next(o for o in self.objects() if o["kind"] == "ConfigMap")
        self.assertEqual(gate["metadata"]["name"], "preview-registry-ready")
        self.assertEqual(gate["metadata"]["labels"]["company.example/registry-gate"], "true")
        self.assertEqual(gate["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-10")
        self.assertEqual(gate["data"], {"state": "Pending", "sourceResourceVersion": ""})
        self.assertNotIn("binaryData", gate)

    def test_registry_permission_overrides_are_rejected(self):
        for key, value in [("registryClusterRole", "cluster-admin"), ("registryControllerNamespace", "production"), ("registryGateState", "Ready")]:
            with self.subTest(key=key):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"][key] = value
                self.reject()

    def test_project_ownership_mismatch_rejected(self):
        self.values["preview"]["sourceProjectId"] = "124"
        self.reject()

    def test_namespace_identity_mismatch_rejected(self):
        self.values["preview"]["namespace"] = "test-example-app-123-99999"
        self.reject()

    def test_invalid_identity_fields_rejected(self):
        cases = [("id", "other-service-123-45678"), ("id", "example-app-0-45678"),
                 ("id", "example-app-123-0"), ("sourceProjectId", "0123"),
                 ("sourceProjectId", 123), ("namespace", "production")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["preview"][key] = value
                self.reject()

    def test_namespace_length_limit_rejected(self):
        self.values["preview"]["id"] = "example-app-123-" + "1" * 50
        self.values["preview"]["namespace"] = "test-" + self.values["preview"]["id"]
        self.reject()

    def test_invalid_timestamp_rejected(self):
        for value in ["2026-11-09", "2026-02-31T18:00:00Z", "2026-11-09T25:00:00Z", "2026-11-09T18:00:00+03:30"]:
            with self.subTest(value=value):
                self.values["preview"]["expiresAt"] = value
                self.reject()

    def test_empty_quota_and_limits_rejected(self):
        for key, value in [("quota", {}), ("limits", [])]:
            with self.subTest(key=key):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"][key] = value
                self.reject()

    def test_negative_quota_rejected(self):
        self.values["provisioning"]["quota"]["pods"] = "-1"
        self.reject()

    def test_foreign_namespace_rejected(self):
        self.values["provisioning"]["resources"][0]["metadata"]["namespace"] = "production"
        self.reject()

    def test_explicit_correct_namespace_accepted(self):
        self.values["provisioning"]["resources"][0]["metadata"]["namespace"] = self.values["preview"]["namespace"]
        self.objects()

    def test_duplicate_resource_rejected(self):
        self.values["provisioning"]["resources"].append(copy.deepcopy(self.values["provisioning"]["resources"][0]))
        self.reject()

    def test_reserved_resource_names_rejected(self):
        for name in ["default-deny", "preview-quota", "preview-limits", "preview-registry-ready", "preview-registry-sync", "default", self.values["preview"]["id"]]:
            with self.subTest(name=name):
                self.values["provisioning"]["resources"][0]["metadata"]["name"] = name
                self.reject()

    def test_forbidden_resource_kinds_rejected(self):
        for kind in ["Namespace", "Secret", "ConfigMap", "Pod", "Job", "Deployment", "RoleBinding", "ClusterRole", "ClusterSecretStore"]:
            with self.subTest(kind=kind):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"]["resources"][0] = {"apiVersion": "v1", "kind": kind, "metadata": {"name": "unapproved"}}
                self.reject()

    def test_wrong_api_version_rejected(self):
        self.values["provisioning"]["resources"][0]["apiVersion"] = "untrusted.example/v1"
        self.reject()

    def test_hooks_and_ownership_annotations_rejected(self):
        for key in ["argocd.argoproj.io/hook", "argocd.argoproj.io/sync-wave", "company.example/preview-id"]:
            with self.subTest(key=key):
                self.values["provisioning"]["resources"][0]["metadata"]["annotations"] = {key: "skip-or-overwrite"}
                self.reject()

    def test_ownership_labels_rejected(self):
        self.values["provisioning"]["resources"][0]["metadata"]["labels"] = {"company.example/preview-id": "someone-else"}
        self.reject()

    def test_owner_references_and_finalizers_rejected(self):
        for key, value in [("ownerReferences", []), ("finalizers", [])]:
            with self.subTest(key=key):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"]["resources"][0]["metadata"][key] = value
                self.reject()

    def test_service_account_cannot_mount_tokens(self):
        self.values["provisioning"]["resources"].append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "preview-secret-reader"}})
        account = next(o for o in self.objects() if o["kind"] == "ServiceAccount")
        self.assertIs(account["automountServiceAccountToken"], False)
        self.values["provisioning"]["resources"][-1]["automountServiceAccountToken"] = True
        self.reject()

    def test_safe_literal_serialization(self):
        literal = '{{ fail "must not execute values as a template" }}'
        self.values["provisioning"]["resources"][0]["metadata"]["annotations"] = {"example.com/literal": literal}
        item = next(o for o in self.objects() if o["metadata"]["name"] == "example-app-approved-traffic")
        self.assertEqual(item["metadata"]["annotations"]["example.com/literal"], literal)

    def test_external_secret_remote_references_and_store_accepted(self):
        self.values["provisioning"]["resources"] += [
            {"apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret", "metadata": {"name": "registry-pull"}, "spec": {"secretStoreRef": {"name": "preview-vault", "kind": "SecretStore"}, "target": {"name": "company-registry"}, "dataFrom": [{"extract": {"key": "preview/registry"}}]}},
            {"apiVersion": "external-secrets.io/v1beta1", "kind": "SecretStore", "metadata": {"name": "preview-vault"}, "spec": {"provider": {"vault": {"server": "https://vault.example.invalid", "path": "preview", "version": "v2", "auth": {"kubernetes": {"role": "preview-pull", "serviceAccountRef": {"name": "preview-secret-reader"}}}}}}},
        ]
        kinds = {o["kind"] for o in self.objects()}
        self.assertIn("ExternalSecret", kinds)
        self.assertIn("SecretStore", kinds)
        self.assertNotIn("Secret", kinds)

    def test_inline_external_secret_template_data_rejected(self):
        self.values["provisioning"]["resources"].append({"apiVersion": "external-secrets.io/v1beta1", "kind": "ExternalSecret", "metadata": {"name": "registry-pull"}, "spec": {"target": {"template": {"data": {}}}}})
        self.reject()

    def test_invalid_secret_and_resource_names_rejected(self):
        for name in ["UPPER", "bad..label", "bad/name", "x" * 64 + ".example"]:
            with self.subTest(name=name):
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"]["requiredSecrets"] = [name]
                self.reject()
                self.values = copy.deepcopy(EXAMPLE)
                self.values["provisioning"]["resources"][0]["metadata"]["name"] = name
                self.reject()

    def test_required_secrets_do_not_emit_secret_values_or_readiness_workload(self):
        self.assertFalse({o["kind"] for o in self.objects()} & {"Secret", "Pod", "Job", "Role", "ClusterRole", "ClusterRoleBinding"})
        self.values["provisioning"]["requiredSecrets"] = ["company-registry", "company-registry"]
        self.reject()


if __name__ == "__main__":
    unittest.main()
