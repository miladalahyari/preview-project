#!/usr/bin/env python3
"""Copy one approved registry credential to Argo-owned preview namespaces."""
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


API_SERVER = "https://kubernetes.default.svc"
SOURCE_NAMESPACE = "previews"
SECRET_NAME = "company-registry"
GATE_NAME = "preview-registry-ready"
MANAGER = "preview-registry-sync"
PREFIX = "company.example/"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
HEALTH_PATH = "/tmp/registry-sync-health.json"
NAMESPACE = re.compile(r"^test-(example-app-([1-9][0-9]*)-([1-9][0-9]*))$")
PROJECT = re.compile(r"^[1-9][0-9]*$")


class ValidationError(RuntimeError):
    pass


class APIError(RuntimeError):
    def __init__(self, status=None):
        self.status = status
        super().__init__("Kubernetes request failed" + (" (HTTP %s)" % status if status else ""))


def require(condition, message):
    if not condition:
        raise ValidationError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise APIError()


class Kubernetes:
    """Only fixed-origin Kubernetes HTTPS requests; never log API bodies."""
    def __init__(self, token_path=TOKEN_PATH, ca_path=CA_PATH, opener=None):
        self.token_path = Path(token_path)
        self.opener = opener or urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_path)))

    def request(self, method, path, body=None, absent=False):
        require(method in {"GET", "POST", "PATCH"}, "Unsupported API operation")
        require(path.startswith("/api/v1/") and not path.startswith("//") and "#" not in path,
                "Unsupported API path")
        try:
            token = self.token_path.read_text().strip()
        except (OSError, UnicodeError):
            raise APIError() from None
        require(token and all(33 <= ord(character) <= 126 for character in token), "Invalid projected credential")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        payload = None if body is None else json.dumps(body).encode()
        if payload is not None:
            headers["Content-Type"] = "application/merge-patch+json" if method == "PATCH" else "application/json"
        request = urllib.request.Request(API_SERVER + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=10) as response:
                value = json.loads(response.read())
                require(isinstance(value, dict), "Invalid Kubernetes response")
                return value
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if absent and method == "GET" and status == 404:
                return None
            raise APIError(status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError() from None
        except (ValueError, UnicodeError):
            raise APIError() from None


def resource_path(namespace, kind, name=None):
    path = "/api/v1/namespaces/" + urllib.parse.quote(namespace, safe="") + "/" + kind
    return path if name is None else path + "/" + urllib.parse.quote(name, safe="")


def identity(namespace, source_project_id):
    metadata = namespace.get("metadata", {})
    match = NAMESPACE.fullmatch(metadata.get("name", ""))
    if not match or match[2] != source_project_id or metadata.get("deletionTimestamp"):
        return None
    labels = metadata.get("labels", {})
    expected = {PREFIX + "preview-id": match[1], PREFIX + "source-project-id": match[2], PREFIX + "ephemeral": "true"}
    if any(labels.get(key) != value for key, value in expected.items()):
        return None
    require(isinstance(metadata.get("uid"), str) and bool(metadata["uid"]), "Namespace UID is required")
    return {"namespace": metadata["name"], "id": match[1], "project": match[2], "uid": metadata["uid"], "labels": expected}


def validate_source(secret):
    metadata = secret.get("metadata", {})
    require(metadata.get("name") == SECRET_NAME and metadata.get("namespace") == SOURCE_NAMESPACE,
            "Unexpected registry source")
    require(secret.get("type") == "kubernetes.io/dockerconfigjson", "Unexpected registry Secret type")
    require(isinstance(metadata.get("resourceVersion"), str) and bool(metadata["resourceVersion"]),
            "Registry source version is required")
    encoded = secret.get("data", {}).get(".dockerconfigjson")
    require(isinstance(encoded, str) and bool(encoded), "Registry source data is required")
    try:
        decoded = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, UnicodeError, binascii.Error):
        raise ValidationError("Registry source data is invalid") from None
    require(isinstance(decoded, dict) and set(decoded) == {"auths"}, "Registry source must contain auths only")
    auths = decoded["auths"]
    require(isinstance(auths, dict) and len(auths) == 1, "Registry source must contain one registry")
    for key, credential in auths.items():
        require(isinstance(key, str) and not any(character.isspace() for character in key), "Invalid registry host")
        try:
            parsed = urllib.parse.urlsplit(key if "://" in key else "https://" + key)
            allowed = (parsed.scheme == "https" and parsed.hostname == "cr.company.example"
                       and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                       and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except ValueError:
            allowed = False
        require(allowed, "Unexpected registry host")
        require(isinstance(credential, dict), "Invalid registry credential structure")
        standalone = any(isinstance(credential.get(name), str) and bool(credential[name])
                         for name in ("auth", "identitytoken", "registrytoken"))
        pair = all(isinstance(credential.get(name), str) and bool(credential[name]) for name in ("username", "password"))
        require(standalone or pair, "Registry credential is missing")
    return {"data": {".dockerconfigjson": encoded}, "version": metadata["resourceVersion"]}


def validate_owned(obj, preview, name, managed=False):
    metadata = obj.get("metadata", {})
    require(metadata.get("name") == name and metadata.get("namespace") == preview["namespace"],
            "Target resource identity mismatch")
    labels = metadata.get("labels", {})
    expected = dict(preview["labels"])
    expected[PREFIX + ("managed-by" if managed else "registry-gate")] = MANAGER if managed else "true"
    require(all(labels.get(key) == value for key, value in expected.items()), "Target resource ownership mismatch")
    require(isinstance(metadata.get("resourceVersion"), str) and bool(metadata["resourceVersion"]),
            "Target resource version is required")
    require(not metadata.get("deletionTimestamp"), "Target resource is terminating")
    require(not obj.get("immutable"), "Target resource is immutable")
    require(not metadata.get("ownerReferences") and not metadata.get("finalizers"), "Target resource has foreign ownership")
    if managed:
        require(set(labels) == set(expected), "Target Secret has additional labels")
        require(not any(key.startswith(("argocd.argoproj.io/", "argocd-image-updater.argoproj.io/"))
                        for key in metadata.get("annotations", {}))
                and "app.kubernetes.io/instance" not in labels, "Target resource is tracked by Argo CD")
    else:
        require(not obj.get("binaryData"), "Readiness gate has unexpected binary data")
        allowed = set(expected) | {"app.kubernetes.io/part-of", "app.kubernetes.io/instance"}
        require(set(labels) <= allowed, "Readiness gate has additional labels")
        require(labels.get("app.kubernetes.io/part-of", "preview-platform") == "preview-platform"
                and labels.get("app.kubernetes.io/instance", preview["id"]) == preview["id"],
                "Readiness gate has foreign tracking labels")
        require(isinstance(obj.get("data"), dict)
                and set(obj["data"]) == {"state", "sourceResourceVersion"}
                and obj["data"]["state"] in {"Pending", "Ready"}
                and isinstance(obj["data"]["sourceResourceVersion"], str)
                and (obj["data"]["state"] != "Ready" or bool(obj["data"]["sourceResourceVersion"])),
                "Readiness gate data is invalid")


class Controller:
    def __init__(self, client, source_project_id, health_path=HEALTH_PATH, log=None):
        require(isinstance(source_project_id, str) and bool(PROJECT.fullmatch(source_project_id)),
                "A positive SOURCE_PROJECT_ID is required")
        self.client, self.project = client, source_project_id
        self.health_path = Path(health_path)
        self.log = log or (lambda event: print(json.dumps(event, sort_keys=True), flush=True))
        self.health = {"lastAttempt": 0, "lastSuccess": None, "sourceReady": False,
                       "eligibleNamespaces": 0, "errors": 0, "waitingNamespaces": 0}

    def heartbeat(self):
        self.health["lastAttempt"] = time.time()
        # Public operational state only; never registry data or projected credentials.
        # Keep the previous complete snapshot visible to concurrent exec probes.
        # The fixed temporary file lives in the controller Pod's private emptyDir.
        if self.health_path.is_symlink():
            raise OSError("Health path must not be a symlink")
        temporary = self.health_path.with_name("." + self.health_path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(self.health, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, self.health_path)

    def event(self, event, error=None, namespace=None):
        value = {"event": event}
        if namespace is not None:
            value["namespace"] = namespace
        if error is not None:
            value["errorType"] = type(error).__name__
            if isinstance(error, APIError) and error.status:
                value["httpStatus"] = error.status
        self.log(value)

    def namespaces(self):
        token = None
        previous = set()
        for _ in range(1000):
            query = {"labelSelector": PREFIX + "ephemeral=true," + PREFIX + "source-project-id=" + self.project,
                     "limit": "200"}
            if token:
                query["continue"] = token
            page = self.client.request("GET", "/api/v1/namespaces?" + urllib.parse.urlencode(query))
            require(isinstance(page.get("items"), list), "Invalid namespace list")
            yield from page["items"]
            token = page.get("metadata", {}).get("continue")
            if not token:
                return
            require(isinstance(token, str) and token not in previous, "Invalid namespace pagination")
            previous.add(token)
        raise ValidationError("Namespace pagination limit exceeded")

    def gate(self, preview):
        gate = self.client.request("GET", resource_path(preview["namespace"], "configmaps", GATE_NAME), absent=True)
        if gate is not None:
            validate_owned(gate, preview, GATE_NAME)
        return gate

    def set_gate(self, preview, state, version=""):
        path = resource_path(preview["namespace"], "configmaps", GATE_NAME)
        desired = {"state": state, "sourceResourceVersion": version}
        for _ in range(3):
            gate = self.gate(preview)
            if gate is None:
                return False
            if gate.get("data") == desired:
                return True
            patch = {"metadata": {"resourceVersion": gate["metadata"]["resourceVersion"]}, "data": desired}
            try:
                updated = self.client.request("PATCH", path, patch)
            except APIError as error:
                if error.status == 409:
                    continue
                raise
            validate_owned(updated, preview, GATE_NAME)
            require(updated.get("data") == desired, "Readiness acknowledgement mismatch")
            return True
        raise APIError(409)

    def sync_secret(self, preview, source):
        path = resource_path(preview["namespace"], "secrets", SECRET_NAME)
        labels = dict(preview["labels"], **{PREFIX + "managed-by": MANAGER})
        for _ in range(3):
            existing = self.client.request("GET", path, absent=True)
            if existing is None:
                desired = {"apiVersion": "v1", "kind": "Secret",
                           "metadata": {"name": SECRET_NAME, "namespace": preview["namespace"], "labels": labels},
                           "type": "kubernetes.io/dockerconfigjson", "data": source["data"]}
                try:
                    actual = self.client.request("POST", resource_path(preview["namespace"], "secrets"), desired)
                except APIError as error:
                    if error.status == 409:
                        continue
                    raise
            else:
                validate_owned(existing, preview, SECRET_NAME, managed=True)
                require(existing.get("type") == "kubernetes.io/dockerconfigjson", "Target Secret type mismatch")
                require(not existing.get("immutable"), "Target Secret is immutable")
                require(set(existing.get("data", {})) == {".dockerconfigjson"}, "Unexpected target Secret data keys")
                if existing.get("data") == source["data"]:
                    return
                patch = {"metadata": {"resourceVersion": existing["metadata"]["resourceVersion"]}, "data": source["data"]}
                try:
                    actual = self.client.request("PATCH", path, patch)
                except APIError as error:
                    if error.status == 409:
                        continue
                    raise
            validate_owned(actual, preview, SECRET_NAME, managed=True)
            require(actual.get("type") == "kubernetes.io/dockerconfigjson" and actual.get("data") == source["data"],
                    "Registry copy verification failed")
            return
        raise APIError(409)

    def reconcile(self, preview, source):
        current = self.client.request("GET", "/api/v1/namespaces/" + urllib.parse.quote(preview["namespace"], safe=""), absent=True)
        current_identity = identity(current, self.project) if current else None
        if current_identity is None or current_identity["uid"] != preview["uid"]:
            return "skipped"
        if self.gate(preview) is None:
            return "waiting"
        try:
            if source is None:
                self.set_gate(preview, "Pending")
                return "waiting"
            self.sync_secret(preview, source)
            return "ready" if self.set_gate(preview, "Ready", source["version"]) else "waiting"
        except (APIError, ValidationError):
            try:
                self.set_gate(preview, "Pending")
            except (APIError, ValidationError) as gate_error:
                self.event("gate-pending-failed", gate_error, preview["namespace"])
            raise

    def run_once(self):
        self.health.update(eligibleNamespaces=0, errors=0, waitingNamespaces=0)
        self.heartbeat()
        source = None
        try:
            source = validate_source(self.client.request("GET", resource_path(SOURCE_NAMESPACE, "secrets", SECRET_NAME)))
            self.health["sourceReady"] = True
        except (APIError, ValidationError) as error:
            self.health["sourceReady"] = False
            self.health["errors"] += 1
            self.event("source-unavailable", error)
        self.heartbeat()
        try:
            for namespace in self.namespaces():
                preview = identity(namespace, self.project)
                if preview is None:
                    continue
                self.health["eligibleNamespaces"] += 1
                try:
                    status = self.reconcile(preview, source)
                    if status == "waiting":
                        self.health["waitingNamespaces"] += 1
                except (APIError, ValidationError) as error:
                    self.health["errors"] += 1
                    self.event("namespace-sync-failed", error, preview["namespace"])
                self.heartbeat()
        except (APIError, ValidationError) as error:
            self.health["errors"] += 1
            self.event("namespace-list-failed", error)
        success = self.health["sourceReady"] and self.health["errors"] == 0
        if success:
            self.health["lastSuccess"] = time.time()
        self.heartbeat()
        return success


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--source-project-id", default=os.environ.get("SOURCE_PROJECT_ID", ""))
    args = parser.parse_args()
    require(5 <= args.interval <= 300, "Interval must be between 5 and 300 seconds")
    controller = Controller(Kubernetes(), args.source_project_id)
    if args.once:
        return 0 if controller.run_once() else 1
    while True:
        controller.run_once()
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (APIError, ValidationError, OSError, ValueError):
        # Avoid exception details: API/credential parsing failures can contain private data.
        print('{"event":"controller-failed"}', file=sys.stderr, flush=True)
        sys.exit(1)
