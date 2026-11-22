#!/usr/bin/env python3
"""Render public Kubernetes controller resources from the reviewed Python source."""
import argparse
import hashlib
import json
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
NAMESPACE = "previews"
ACCOUNT = "preview-registry-sync"

class Dumper(yaml.SafeDumper):
    pass

def string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
Dumper.add_representer(str, string)

def metadata(name, namespace=None, wave=-20):
    value = {"name": name, "annotations": {"argocd.argoproj.io/sync-wave": str(wave)}}
    if namespace:
        value["namespace"] = namespace
    return value

def render():
    source = (ROOT / "registry_sync.py").read_text()
    image = json.loads((ROOT.parents[1] / "tools/image-lock.json").read_text())["image"]
    checksum = hashlib.sha256(source.encode()).hexdigest()
    subject = {"kind": "ServiceAccount", "name": ACCOUNT, "namespace": NAMESPACE}
    resources = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata(ACCOUNT, NAMESPACE), "automountServiceAccountToken": True},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": metadata("preview-registry-source", NAMESPACE),
         "rules": [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["company-registry"], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": metadata("preview-registry-source", NAMESPACE, -10),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "preview-registry-source"}, "subjects": [subject]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": metadata("preview-registry-namespaces"),
         "rules": [{"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get", "list"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": metadata("preview-registry-namespaces", wave=-10),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "preview-registry-namespaces"}, "subjects": [subject]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": metadata("preview-registry-target"),
         "rules": [
             {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["company-registry"], "verbs": ["get", "patch"]},
             {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
             {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["preview-registry-ready"], "verbs": ["get", "patch"]},
         ]},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata("preview-registry-script", NAMESPACE, -10), "data": {"registry_sync.py": source}},
    ]
    health = "/tmp/registry-sync-health.json"
    liveness = "import json,time; h=json.load(open('" + health + "')); assert time.time()-h['lastAttempt']<90"
    readiness = "import json,time; h=json.load(open('" + health + "')); assert time.time()-h['lastAttempt']<60 and h['sourceReady'] and h['errors']==0"
    resources.append({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(ACCOUNT, NAMESPACE, 0),
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app.kubernetes.io/name": ACCOUNT}},
                 "template": {"metadata": {"labels": {"app.kubernetes.io/name": ACCOUNT}, "annotations": {"company.example/script-sha256": checksum}},
                              "spec": {"serviceAccountName": ACCOUNT, "automountServiceAccountToken": True,
                                       "imagePullSecrets": [{"name": "company-registry"}],
                                       "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}},
                                       "containers": [{"name": "sync", "image": image, "imagePullPolicy": "Always",
                                                       "command": ["python3", "/opt/preview-registry/registry_sync.py"],
                                                       "env": [{"name": "SOURCE_PROJECT_ID", "value": "100"}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                                                       "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                                                       "resources": {"requests": {"cpu": "50m", "memory": "48Mi"}, "limits": {"cpu": "250m", "memory": "128Mi"}},
                                                       "volumeMounts": [{"name": "script", "mountPath": "/opt/preview-registry", "readOnly": True}, {"name": "runtime", "mountPath": "/tmp"}],
                                                       "startupProbe": {"exec": {"command": ["python3", "-c", liveness]}, "periodSeconds": 5, "timeoutSeconds": 5, "failureThreshold": 18},
                                                       "livenessProbe": {"exec": {"command": ["python3", "-c", liveness]}, "initialDelaySeconds": 30, "periodSeconds": 20, "timeoutSeconds": 5, "failureThreshold": 3},
                                                       "readinessProbe": {"exec": {"command": ["python3", "-c", readiness]}, "initialDelaySeconds": 5, "periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 3}}],
                                       "volumes": [{"name": "script", "configMap": {"name": "preview-registry-script"}}, {"name": "runtime", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]}}}
    })
    return "# Generated from platform/registry-sync; contains no registry credentials.\n" + yaml.dump_all(resources, Dumper=Dumper, sort_keys=False)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    destination = parser.parse_args().output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render())
    print("Rendered controller resources:", destination)

if __name__ == "__main__":
    main()
