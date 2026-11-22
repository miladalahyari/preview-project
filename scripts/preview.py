#!/usr/bin/env python3
"""Trusted tag-preview lifecycle. State writes use GitLab's atomic commits API."""
import argparse
import base64
import copy
import datetime as dt
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

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/platform.json").read_text())
API_URL = CONFIG["gitlabApiUrl"]
CENTRAL_PATH = CONFIG["centralProjectPath"]
STATE_REF = CONFIG["stateRef"]
CENTRAL_PROJECT_ID = CONFIG["centralProjectId"]
MAX_PROJECT_PREVIEWS = 3
TAG = re.compile(r"^test[-.]?[a-z0-9]+([._-][a-z0-9]+)*$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
BOOTSTRAP_REPOSITORY = CONFIG["gitopsRepository"]
BOOTSTRAP_PATH = CONFIG["bootstrapChartPath"]
WORKLOAD_PATH = CONFIG["workloadChartPath"]
ARGOCD_PROJECT = CONFIG["argocdProject"]
ARGOCD_SERVER = CONFIG["argocdServer"]


class Failure(RuntimeError):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        previous, target = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(new_url)
        require(target.scheme == "https" and not target.username and not target.password, "Unsafe API redirect")
        require(request.get_method() == "GET", "Only read requests may redirect")
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if (previous.hostname, previous.port or 443) != (target.hostname, target.port or 443):
            for values in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(values):
                    if key.lower() in {"private-token", "job-token", "authorization"}:
                        del values[key]
        return redirected


def now():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def instant(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def require(condition, message):
    if not condition:
        raise Failure(message)


def quoted(value):
    return urllib.parse.quote(str(value), safe="")


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


class GitLab:
    def __init__(self, token, job_token=None):
        require(bool(token), "PREVIEW_GITLAB_TOKEN or HELM_TAG_TOKEN is required")
        require(isinstance(token, str) and all(33 <= ord(character) <= 126 for character in token), "GitLab API credential has invalid characters")
        require(job_token is None or (isinstance(job_token, str) and bool(job_token) and all(33 <= ord(character) <= 126 for character in job_token)), "GitLab job credential has invalid characters")
        self.token = token
        self.job_token = job_token
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, method, path, data=None, absent=False, raw=False, job=False):
        headers = {"JOB-TOKEN" if job else "PRIVATE-TOKEN": self.job_token if job else self.token}
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API_URL + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=45) as response:
                content = response.read()
                return content if raw else (json.loads(content) if content else None)
        except urllib.error.HTTPError as error:
            if absent and error.code == 404:
                return None
            # API response bodies may contain private data; keep failure logs bounded.
            raise Failure(f"GitLab {method} {path.split('?')[0]} returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise Failure(f"GitLab request failed: {type(error).__name__}") from None

    def pages(self, path):
        result = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 1001):
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            require(isinstance(batch, list), "Expected paginated GitLab array")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise Failure("GitLab pagination limit exceeded")

    def current_identity(self):
        # /job binds identity to the actual job token, not overridable forwarded CI variables.
        require(bool(self.job_token), "CI_JOB_TOKEN is required for pipeline operations")
        identity = self.request("GET", "/job", job=True)
        project = identity["pipeline"]["project_id"] if "project_id" in identity["pipeline"] else identity["project"]["id"]
        pipeline = identity["pipeline"]["id"]
        actual_project = self.request("GET", f"/projects/{project}")
        actual_pipeline = self.request("GET", f"/projects/{project}/pipelines/{pipeline}")
        require(actual_project["path_with_namespace"] == CENTRAL_PATH, "Run only in the configured central project")
        require(actual_pipeline["ref"] == STATE_REF, "Trusted pipeline must execute on main")
        branch = self.request("GET", f"/projects/{project}/repository/branches/{STATE_REF}")
        require(branch.get("protected") is True, "Central main branch must be protected")
        require(identity.get("ref", actual_pipeline["ref"]) == STATE_REF, "Unexpected job ref")
        self.identity, self.pipeline = identity, actual_pipeline
        return str(project), str(pipeline)

    def cluster_reaper_identity(self):
        """A dedicated cluster Job uses protected main and a Maintainer API token."""
        project = self.request("GET", f"/projects/{CENTRAL_PROJECT_ID}")
        require(project.get("path_with_namespace") == CENTRAL_PATH, "Unexpected central project")
        branch = self.request("GET", f"/projects/{CENTRAL_PROJECT_ID}/repository/branches/{STATE_REF}")
        require(branch.get("protected") is True, "Central main branch must be protected")
        require(branch.get("commit", {}).get("id") == os.environ.get("PREVIEW_CODE_REVISION"),
                "CronJob code revision is no longer current; retry on the next run")
        user = self.request("GET", "/user")
        actor = str(user.get("id", ""))
        require(re.fullmatch(r"[1-9][0-9]*", actor), "GitLab API token has no user identity")
        member = self.request("GET", f"/projects/{CENTRAL_PROJECT_ID}/members/all/{actor}")
        require(isinstance(member.get("access_level"), int) and member["access_level"] >= 40,
                "CronJob GitLab token requires central Maintainer access")
        return CENTRAL_PROJECT_ID, "0"


class Store:
    def __init__(self, api, project):
        self.api, self.project, self.versions = api, project, {}

    def read(self, path):
        item = self.api.request("GET", f"/projects/{self.project}/repository/files/{quoted(path)}?ref={STATE_REF}", absent=True)
        if item is None:
            self.versions[path] = None
            return None
        self.versions[path] = item["last_commit_id"]
        return json.loads(base64.b64decode(item["content"]))

    def paths(self, directory):
        items = self.api.pages(f"/projects/{self.project}/repository/tree?ref={STATE_REF}&recursive=true")
        return [item["path"] for item in items if item["type"] == "blob" and item["path"].startswith(directory + "/") and item["path"].endswith((".yaml", ".json"))]

    def records(self, directory):
        return [(path, self.read(path)) for path in self.paths(directory)]

    def commit(self, updates, message):
        actions = []
        for path, value in updates.items():
            if path not in self.versions:
                self.read(path)
            version = self.versions[path]
            if value is None and version is None:
                continue
            action = {"file_path": path, "action": "delete" if value is None else ("update" if version else "create")}
            if value is not None:
                action["content"] = json_text(value)
            if version:
                action["last_commit_id"] = version
            actions.append(action)
        if actions:
            self.api.request("POST", f"/projects/{self.project}/repository/commits", {"branch": STATE_REF, "commit_message": message + " [skip ci]", "actions": actions})
            for path in updates:
                self.versions.pop(path, None)


class NoArgoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Failure("Argo CD redirects are not allowed")


class ArgoCD:
    """Read-only observer. Deployment and deletion happen through Git records."""
    def __init__(self, server, token):
        require(isinstance(server, str) and server.rstrip("/") == ARGOCD_SERVER, "ARGOCD_SERVER must be the approved HTTPS endpoint")
        require(isinstance(token, str) and bool(token) and all(33 <= ord(character) <= 126 for character in token), "A valid read-only ARGOCD_AUTH_TOKEN is required")
        self.server, self.token = server.rstrip("/"), token
        self.opener = urllib.request.build_opener(NoArgoRedirect())

    def application(self, name):
        require(bool(LABEL.fullmatch(name)) and len(name) <= 53 and name.startswith("example-app-"), "Invalid preview Application name")
        request = urllib.request.Request(
            self.server + "/api/v1/applications/" + quoted(name) + "?project=" + ARGOCD_PROJECT + "&appNamespace=argocd",
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                application = json.loads(response.read())
                require(isinstance(application, dict), "Argo CD returned an invalid Application")
                return application
        except urllib.error.HTTPError as error:
            if error.code == 404:
                try:
                    body = json.loads(error.read())
                except (ValueError, UnicodeError):
                    body = None
                if isinstance(body, dict) and body.get("code") == 5:
                    return None
            raise Failure(f"Argo CD Application lookup returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise Failure("Argo CD Application lookup failed; retry without releasing ownership") from None
        except (ValueError, UnicodeError):
            raise Failure("Argo CD returned invalid JSON") from None


class KubernetesArgo:
    """The cluster reaper observes Applications with its scoped ServiceAccount."""
    def __init__(self):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        try:
            self.token = (directory / "token").read_text().strip()
            require(bool(self.token), "Kubernetes ServiceAccount token is empty")
            context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        except (OSError, ValueError) as error:
            raise Failure("Kubernetes ServiceAccount credential is unavailable") from None
        self.opener = urllib.request.build_opener(NoArgoRedirect(), urllib.request.HTTPSHandler(context=context))

    def application(self, name):
        require(bool(LABEL.fullmatch(name)) and len(name) <= 53 and name.startswith("example-app-"),
                "Invalid preview Application name")
        request = urllib.request.Request(
            "https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/" + quoted(name),
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.loads(response.read())
                require(isinstance(result, dict), "Kubernetes returned an invalid Application")
                return result
        except urllib.error.HTTPError as error:
            if error.code == 404:
                try:
                    body = json.loads(error.read())
                except (ValueError, UnicodeError):
                    body = None
                details = body.get("details", {}) if isinstance(body, dict) else {}
                if (isinstance(body, dict) and body.get("reason") == "NotFound" and details.get("name") == name
                        and details.get("group") == "argoproj.io" and details.get("kind") == "applications"):
                    return None
            raise Failure(f"Kubernetes Application lookup returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeError):
            raise Failure("Kubernetes Application lookup failed; retry without releasing ownership") from None


def provisioning_values(preview, catalog):
    settings = catalog["provisioning"]
    require(settings.get("approved") is True and settings.get("dependenciesReviewed") is True, "Namespace dependencies/policies must be approved before enabling")
    require(settings.get("registrySecretDelivery") is True, "The approved registry-secret delivery gate is required")
    policies = settings.get("networkPolicyFiles", [])
    resources = copy.deepcopy(settings.get("resolvedResources", []))
    require(bool(policies) and len(resources) == len(policies + settings.get("secretIntegrationFiles", [])), "Pinned provisioning resources are missing")
    for resource in resources:
        require(isinstance(resource, dict) and resource.get("kind") in {"NetworkPolicy", "ExternalSecret", "SecretStore", "ServiceAccount"}, "Unsupported provisioning resource")
        metadata = resource.get("metadata", {})
        require(isinstance(metadata, dict) and bool(metadata.get("name")), "Provisioning resource name is required")
        require(metadata.get("namespace", preview["namespace"]) == preview["namespace"], "Provisioning namespace mismatch")
        metadata["namespace"] = preview["namespace"]
    required_secrets = settings.get("requiredSecrets", [])
    require(isinstance(required_secrets, list), "Required Secrets must be a list")
    for secret in required_secrets:
        require(isinstance(secret, str) and len(secret) <= 253 and all(LABEL.fullmatch(part) and len(part) <= 63 for part in secret.split(".")), "Invalid required Secret name")
    require(isinstance(settings.get("quota"), dict) and isinstance(settings.get("limits"), list), "Approved quota and limits are required")
    return {"quota": copy.deepcopy(settings["quota"]), "limits": copy.deepcopy(settings["limits"]), "resources": resources, "requiredSecrets": list(required_secrets), "registrySecretDelivery": True}


def source_input(env):
    fields = {name: env.get("PREVIEW_SOURCE_" + key, "") for name, key in (("sourceProjectId", "PROJECT_ID"), ("sourcePipelineId", "PIPELINE_ID"), ("sourceSha", "SHA"), ("sourceTag", "TAG"))}
    require(fields["sourceProjectId"].isdigit() and fields["sourcePipelineId"].isdigit(), "Positive source project/pipeline IDs required")
    require(int(fields["sourceProjectId"]) > 0 and int(fields["sourcePipelineId"]) > 0, "Positive source IDs required")
    require(bool(SHA.fullmatch(fields["sourceSha"])), "Full source commit SHA required")
    require(len(fields["sourceTag"]) <= 128 and bool(TAG.fullmatch(fields["sourceTag"])), "Invalid test tag")
    return fields


def validate_catalog(catalog, source):
    require(str(catalog["source"]["projectId"]) == source["sourceProjectId"], "Catalog source project mismatch")
    require(catalog.get("enabled") is True, "Catalog service is not enabled")
    require(bool(SHA.fullmatch(catalog["chart"]["approvedRevision"])), "Approved full Helm commit SHA required")
    require(catalog["chart"]["repository"] == BOOTSTRAP_REPOSITORY, "Unapproved Helm repository")
    require(catalog["endpoint"]["type"] in {"http", "worker", "batch"}, "Unsupported endpoint type")
    require(catalog["lifecycle"]["ttlHours"] == 12, "Expected 12-hour preview policy")
    require(catalog["service"] == "example-app" and catalog["chart"]["path"] == WORKLOAD_PATH, "Only the reviewed example-app adapter is implemented")
    require(catalog["adapter"]["imageDigestKey"] == "image.digest", "Unsupported image adapter")
    require(catalog["adapter"]["hostKey"] == "ingress.hosts[0].host", "Unsupported host adapter")
    provisioning = catalog.get("provisioning", {})
    require(provisioning.get("repository") == BOOTSTRAP_REPOSITORY and provisioning.get("path") == BOOTSTRAP_PATH, "Unapproved namespace bootstrap chart")
    require(bool(SHA.fullmatch(provisioning.get("approvedRevision", ""))), "Approved full namespace bootstrap commit SHA required")
    return catalog


def choose_catalog(source, selection, root=ROOT):
    definitions = [json.loads(path.read_text()) for path in (root / "catalog").glob("*.json")]
    matches = [entry for entry in definitions if str(entry["source"]["projectId"]) == source["sourceProjectId"] and (not selection or entry["service"] == selection)]
    require(len(matches) == 1, "Source project/service must match exactly one catalog entry")
    return validate_catalog(matches[0], source)


def environment_name(preview):
    require(preview["service"] == "example-app", "Only the reviewed example-app environment mapping is supported")
    require(preview["sourceProjectId"].isdigit(), "Environment source project ID is invalid")
    require(bool(SHA.fullmatch(preview["sourceSha"])), "Environment source SHA is invalid")
    return f"test/{preview['service']}-{preview['sourceProjectId']}-{preview['sourceSha']}"


def make_preview(source, catalog, at, revision):
    service = catalog["service"]
    require(bool(LABEL.fullmatch(service)), "Invalid catalog service key")
    identifier = f"{service}-{source['sourceProjectId']}-{source['sourcePipelineId']}"
    namespace = f"test-{identifier}"
    require(len(identifier) <= 53 and len(namespace) <= 63, "Helm/Kubernetes names exceed limits")
    host = None
    if catalog["endpoint"]["type"] == "http":
        suffix = catalog["endpoint"]["hostname"]
        left = f"test-{source['sourceSha'][:3]}-{suffix}"
        require(bool(LABEL.fullmatch(left)) and len(left) <= 63, "Invalid preview hostname")
        host = left + "." + CONFIG["previewDomain"]
    result = dict(source, service=service, id=identifier, namespace=namespace, host=host, imageRepository=catalog["image"]["repository"], buildImageTag=f"preview-{source['sourceProjectId']}-{source['sourcePipelineId']}", chartRevision=catalog["chart"]["approvedRevision"], bootstrapRevision=catalog["provisioning"]["approvedRevision"], catalogRevision=revision, createdAt=timestamp(at), setupDeadline=timestamp(at + dt.timedelta(minutes=30)), expiresAt="")
    result["environmentName"] = environment_name(result)
    return result


def record_path(directory, preview):
    return f"{directory}/{preview['service']}/{preview['sourceProjectId']}-{preview['sourcePipelineId']}.yaml"


def request_path(source):
    return f"requests/{source['sourceProjectId']}-{source['sourcePipelineId']}.json"


def inspect_reservations(records, preview):
    for path, record in records:
        existing = record["preview"]
        same_tag = existing["service"] == preview["service"] and existing["sourceProjectId"] == preview["sourceProjectId"] and existing["sourceTag"] == preview["sourceTag"]
        if same_tag:
            require(existing["sourceSha"] == preview["sourceSha"], "Moving a reserved source tag is forbidden")
            require(record["status"] != "cleaning", "Preview is cleaning; wait for completion")
            return path, record
        if preview["host"] and existing.get("host") == preview["host"]:
            raise Failure("Complete preview hostname is already reserved")
    return None


def occupied_project_previews(records, project):
    """Count reservations/namespaces, not request aliases or finished teardown."""
    occupied = set()
    for path, record in records:
        preview = record["preview"]
        if str(preview["sourceProjectId"]) != str(project):
            continue
        # These phases follow confirmed Argo absence, or cleanup of an unpublished
        # reservation. A separate watched record still holds its own slot below.
        if path.startswith("cleanup/") and record.get("phase") in {"tags", "environment"}:
            continue
        occupied.add(preview["id"])
    return occupied


class Lifecycle:
    def __init__(self, api, store, argo, central_project, central_pipeline):
        self.api, self.store, self.argo = api, store, argo
        self.central_project, self.central_pipeline = central_project, central_pipeline

    def verified_source(self, env, verify_tag=True):
        source = source_input(env)
        project, pipeline = source["sourceProjectId"], source["sourcePipelineId"]
        details = self.api.request("GET", f"/projects/{project}/pipelines/{pipeline}")
        require(details["sha"] == source["sourceSha"] and details["ref"] == source["sourceTag"], "Source pipeline ref/SHA mismatch")
        bridges = self.api.pages(f"/projects/{project}/pipelines/{pipeline}/bridges")
        # Pipeline IDs are instance-wide. GitLab's bridge response need not include project_id.
        require(any(str((item.get("downstream_pipeline") or {}).get("id")) == self.central_pipeline for item in bridges), "No authenticated source bridge to this central pipeline")
        if verify_tag:
            tag = self.api.request("GET", f"/projects/{project}/repository/tags/{quoted(source['sourceTag'])}")
            require(tag["commit"]["id"] == source["sourceSha"], "Source tag no longer points to source SHA")
        return source

    def verify_catalog_project(self, catalog):
        actual = self.api.request("GET", f"/projects/{quoted(catalog['source']['projectId'])}")
        require(actual["path_with_namespace"] == catalog["source"]["projectPath"], "Catalog project path/ID mismatch")

    def all_records(self):
        return sum((self.store.records(directory) for directory in ("pending", "previews", "cleanup")), [])

    def prepare(self, env):
        source = self.verified_source(env)
        old_request = self.store.read(request_path(source))
        require(old_request is None or old_request["status"] != "stopped", "Stopped requests cannot renew their lease; create a fresh test tag")
        catalog = choose_catalog(source, env.get("PREVIEW_SERVICE", ""))
        self.verify_catalog_project(catalog)
        revision = self.api.request("GET", f"/projects/{self.central_project}/pipelines/{self.central_pipeline}")["sha"]
        candidate = make_preview(source, catalog, now(), revision)
        records = self.all_records()
        existing = inspect_reservations(records, candidate)
        if existing:
            path, record = existing
            require(record["status"] != "provisioning", "Preview is provisioning; retry prepare after registration finishes")
            require(not (record["status"] == "pending" and record["preview"]["sourcePipelineId"] != source["sourcePipelineId"]), "Same tag is being prepared by another pipeline")
            require(not (record["status"] == "pending" and instant(record["preview"]["setupDeadline"]) <= now()), "Pending setup has expired; cleanup must finish first")
            require(not (record["status"] == "active" and instant(record["preview"]["expiresAt"]) <= now()), "Existing preview is expired; cleanup must finish first")
            candidate = record["preview"]
            request = {"buildRequired": record["status"] == "pending", "status": record["status"], "recordPath": path, "preview": candidate}
            self.store.commit({request_path(source): request}, "preview: return existing registration")
        else:
            occupied = occupied_project_previews(records, source["sourceProjectId"])
            require(len(occupied) < MAX_PROJECT_PREVIEWS,
                    f"Source project {source['sourceProjectId']} has reached its {MAX_PROJECT_PREVIEWS}-preview limit; stop a preview and wait for namespace teardown")
            path = record_path("pending", candidate)
            request = {"buildRequired": True, "status": "pending", "recordPath": path, "preview": candidate}
            self.store.commit({path: {"status": "pending", "preview": candidate}, request_path(source): request}, "preview: reserve hostname before build")
        return request

    def build_metadata(self, source, catalog):
        project, pipeline = source["sourceProjectId"], source["sourcePipelineId"]
        jobs = self.api.pages(f"/projects/{project}/pipelines/{pipeline}/jobs")
        jobs = [job for job in jobs if job["name"] == "build:test" and job["status"] == "success"]
        require(bool(jobs), "No successful source build:test job")
        job = max(jobs, key=lambda item: item["id"])
        require(job["commit"]["id"] == source["sourceSha"], "Build job SHA mismatch")
        content = self.api.request("GET", f"/projects/{project}/jobs/{job['id']}/artifacts/preview-build.json", raw=True)
        metadata = json.loads(content)
        for key, value in source.items():
            require(str(metadata.get(key)) == value, f"Build metadata mismatch: {key}")
        require(metadata.get("imageRepository") == catalog["image"]["repository"], "Build image repository mismatch")
        expected_tag = f"preview-{project}-{pipeline}"
        require(metadata.get("buildImageTag") == expected_tag, "Build image tag mismatch")
        require(bool(DIGEST.fullmatch(metadata.get("imageDigest", ""))), "Build digest is invalid")
        repositories = self.api.pages(f"/projects/{project}/registry/repositories")
        matches = [repo for repo in repositories if repo.get("location") == catalog["image"]["repository"]]
        require(len(matches) == 1, "Expected one approved registry repository")
        repository = matches[0]
        tag = self.api.request("GET", f"/projects/{project}/registry/repositories/{repository['id']}/tags/{quoted(expected_tag)}")
        require(tag["digest"] == metadata["imageDigest"], "Registry digest differs from build artifact")
        return metadata["imageDigest"], str(repository["id"])

    def pinned_catalog(self, preview):
        item = self.api.request("GET", f"/projects/{self.central_project}/repository/files/{quoted('catalog/' + preview['service'] + '.json')}?ref={preview['catalogRevision']}")
        catalog = validate_catalog(json.loads(base64.b64decode(item["content"])), preview)
        settings = catalog.get("provisioning", {})
        resolved = []
        for relative in settings.get("networkPolicyFiles", []) + settings.get("secretIntegrationFiles", []):
            path = Path(relative)
            require(not path.is_absolute() and path.parts[0] == "provisioning" and ".." not in path.parts, "Provisioning file must be inside provisioning/")
            resource = self.api.request("GET", f"/projects/{self.central_project}/repository/files/{quoted(relative)}?ref={preview['catalogRevision']}")
            resolved.append(json.loads(base64.b64decode(resource["content"])))
        settings["resolvedResources"] = resolved
        return catalog

    def register(self, env):
        source = self.verified_source(env)
        request = self.store.read(request_path(source))
        require(request is not None, "Run trusted prepare before build")
        preview = request["preview"]
        record = self.store.read(request["recordPath"])
        require(record is not None and record["status"] in {"pending", "provisioning", "active"}, "Preview no longer eligible for registration")
        preview = record["preview"]
        catalog = self.pinned_catalog(preview)
        self.verify_catalog_project(catalog)
        require(preview["chartRevision"] == catalog["chart"]["approvedRevision"], "Pinned catalog chart mismatch")
        require(preview["bootstrapRevision"] == catalog["provisioning"]["approvedRevision"], "Pinned namespace bootstrap chart mismatch")
        if record["status"] == "active":
            require(instant(preview["expiresAt"]) > now(), "Preview lease is expired")
            # A duplicate source-tag pipeline reuses the canonical preview; it never rebuilds.
            request.update(buildRequired=False, status="active", preview=preview)
            self.store.commit({request_path(source): request}, "preview: reuse immutable active deployment")
            return request
        require(preview["sourcePipelineId"] == source["sourcePipelineId"], "Pending preview belongs to another pipeline")
        require(instant(preview["setupDeadline"]) > now(), "Pending setup deadline exceeded")
        if record["status"] == "provisioning":
            require(bool(DIGEST.fullmatch(preview.get("imageDigest", ""))), "Provisioning record has no verified image")
            request.update(buildRequired=False, status="provisioning", preview=preview)
            self.store.commit({request_path(source): request}, "preview: resume immutable GitOps provisioning")
            return request
        digest, repository_id = self.build_metadata(source, catalog)
        require(not preview.get("imageDigest") or preview["imageDigest"] == digest,
                "Cannot replace a previously verified preview image digest")
        require(not preview.get("registryRepositoryId") or preview["registryRepositoryId"] == repository_id,
                "Cannot replace the registered registry repository")
        preview.update(imageDigest=digest, registryRepositoryId=repository_id, provisioning=provisioning_values(preview, catalog), expiresAt="")
        deployment_path = record_path("previews", preview)
        pending_path = request["recordPath"]
        request.update(buildRequired=False, status="provisioning", recordPath=deployment_path, preview=preview)
        # One atomic Git transaction publishes both namespace and workload inputs to Argo CD.
        self.store.commit({pending_path: None, deployment_path: {"status": "provisioning", "preview": preview}, request_path(source): request}, "preview: publish verified GitOps deployment")
        return request

    def find_request(self, env):
        # Stop uses persisted source IDs, not a source checkout, tag, or build artifact.
        project, pipeline = env.get("PREVIEW_SOURCE_PROJECT_ID", ""), env.get("PREVIEW_SOURCE_PIPELINE_ID", "")
        require(project.isdigit() and pipeline.isdigit(), "Source IDs required")
        request = self.store.read(f"requests/{project}-{pipeline}.json")
        require(request is not None, "Preview request is unknown")
        return request

    def ready(self, env):
        self.verified_source(env)
        request = self.find_request(env)
        preview = request["preview"]
        require(request["status"] in {"provisioning", "active"}, "Preview request is not eligible for readiness")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            record = self.store.read(record_path("previews", preview))
            require(record is not None and record["status"] in {"provisioning", "active"}, "Preview was stopped during readiness")
            preview = record["preview"]
            expiry = preview["expiresAt"] if record["status"] == "active" else preview["setupDeadline"]
            require(instant(expiry) > now(), "Preview deadline expired during readiness")
            application = self.argo.application(preview["id"])
            if application is not None and self.application_ready(application, preview):
                request.update(status=record["status"], preview=preview)
                return request
            time.sleep(10)
        raise Failure("Argo CD readiness timeout; tracked setup/lease deadline remains due for cleanup")

    def verify_application(self, application, preview):
        metadata, spec = application.get("metadata", {}), application.get("spec", {})
        require(metadata.get("name") == preview["id"] and metadata.get("namespace") == "argocd", "Argo Application identity mismatch")
        require("resources-finalizer.argocd.argoproj.io" in metadata.get("finalizers", []), "Foreground Application resource finalizer is required")
        require(spec.get("project") == ARGOCD_PROJECT, "Argo project mismatch")
        require(spec.get("destination", {}).get("namespace") == preview["namespace"], "Argo destination mismatch")
        require(spec.get("destination", {}).get("server") == "https://kubernetes.default.svc", "Argo cluster mismatch")
        sources = spec.get("sources", [])
        require(len(sources) == 2 and not spec.get("source"), "Expected namespace bootstrap and workload sources")
        for source, repository, path, revision in (
            (sources[0], BOOTSTRAP_REPOSITORY, BOOTSTRAP_PATH, preview["bootstrapRevision"]),
            (sources[1], BOOTSTRAP_REPOSITORY, WORKLOAD_PATH, preview["chartRevision"]),
        ):
            require(set(source) == {"repoURL", "path", "targetRevision", "helm"}, "Argo source contains unsupported overrides")
            require(source.get("repoURL") == repository and source.get("path") == path, "Argo chart source mismatch")
            require(source.get("targetRevision") == revision, "Argo chart revision mismatch")
        bootstrap = sources[0]["helm"]
        require(set(bootstrap) == {"releaseName", "values"} and bootstrap["releaseName"] == preview["id"], "Argo bootstrap Helm overrides are not approved")
        try:
            values = json.loads(bootstrap["values"])
        except (ValueError, TypeError):
            raise Failure("Argo bootstrap values must be the expected JSON object") from None
        expected = {"preview": {key: preview[key] for key in ("id", "namespace", "sourceProjectId", "expiresAt")}, "provisioning": preview["provisioning"]}
        require(values == expected, "Argo bootstrap namespace/provisioning values mismatch")
        workload = sources[1]["helm"]
        require(set(workload) == {"releaseName", "valueFiles", "parameters"}
                and workload["releaseName"] == preview["id"] and workload["valueFiles"] == ["values-test.yaml"], "Argo workload Helm overrides are not approved")
        expected_parameters = {"fullnameOverride": preview["id"], "image.digest": preview["imageDigest"], "ingress.hosts[0].host": preview["host"]}
        parameters = workload["parameters"]
        require(isinstance(parameters, list) and len(parameters) == len(expected_parameters), "Argo workload image/host parameters mismatch")
        actual = {}
        for item in parameters:
            require(set(item) == {"name", "value", "forceString"} and item["forceString"] is True, "Argo workload parameters must be approved strings")
            actual[item["name"]] = item["value"]
        require(actual == expected_parameters, "Argo workload image/host parameters mismatch")

    def application_ready(self, application, preview):
        self.verify_application(application, preview)
        status = application.get("status", {})
        gates = [resource for resource in status.get("resources", [])
                 if resource.get("group", "") == "" and resource.get("kind") == "ConfigMap"
                 and resource.get("name") == "preview-registry-ready"
                 and resource.get("namespace") == preview["namespace"]]
        return (not application.get("metadata", {}).get("deletionTimestamp")
                and status.get("sync", {}).get("status") == "Synced"
                and status.get("health", {}).get("status") == "Healthy"
                and status.get("sync", {}).get("revisions") == [preview["bootstrapRevision"], preview["chartRevision"]]
                and status.get("sync", {}).get("comparedTo", {}).get("sources") == application["spec"]["sources"]
                and len(gates) == 1 and gates[0].get("status") == "Synced"
                and gates[0].get("health", {}).get("status") == "Healthy")

    def activate(self, env):
        source = self.verified_source(env)
        request = self.find_request(env)
        path = record_path("previews", request["preview"])
        record = self.store.read(path)
        require(record is not None and record["status"] in {"provisioning", "active"}, "Stopped or cleaning previews cannot activate")
        preview = record["preview"]
        expiry = preview["expiresAt"] if record["status"] == "active" else preview["setupDeadline"]
        require(instant(expiry) > now(), "Preview activation deadline expired")
        application = self.argo.application(preview["id"])
        require(application is not None and self.application_ready(application, preview), "Application is not ready at activation; retry after Argo sync")
        if record["status"] == "active":
            request.update(buildRequired=False, status="active", preview=preview)
            return request
        preview["environmentName"] = environment_name(preview)
        environment_data = {"name": preview["environmentName"]}
        if preview.get("host"):
            environment_data["external_url"] = "https://" + preview["host"] + "/"
        existing = self.api.pages(f"/projects/{self.central_project}/environments?name={quoted(environment_data['name'])}")
        exact = [item for item in existing if item.get("name") == preview["environmentName"]]
        require(len(exact) <= 1, "Ambiguous central environment")
        environment = exact[0] if exact else self.api.request("POST", f"/projects/{self.central_project}/environments", environment_data)
        preview.update(environmentId=str(environment["id"]), activatedAt=timestamp(now()), expiresAt=timestamp(now() + dt.timedelta(hours=12)))
        request.update(buildRequired=False, status="active", recordPath=path, preview=preview)
        self.store.commit({path: {"status": "active", "preview": preview}, request_path(source): request}, "preview: activate 12-hour lease after Argo readiness")
        return request

    def begin_cleanup(self, path, record, reason):
        preview = record["preview"]
        cleanup_path = record_path("cleanup", preview)
        published = path.startswith("previews/")
        updated = dict(record, status="cleaning", phase="observe" if published else "tags", reason=reason, cleanupStartedAt=timestamp(now()))
        updates = {path: {"status": "cleaning", "preview": preview} if published else None, cleanup_path: updated}
        for req_path, request in self.store.records("requests"):
            if request["preview"]["id"] == preview["id"]:
                request.update(status="cleaning", buildRequired=False)
                updates[req_path] = request
        self.store.commit(updates, "preview: begin " + reason + " cleanup")
        return cleanup_path, updated

    def shared_digest(self, preview):
        for directory in ("previews", "pending"):
            for _, record in self.store.records(directory):
                other = record["preview"]
                if other["id"] != preview["id"] and other.get("imageRepository") == preview["imageRepository"] and other.get("imageDigest") == preview.get("imageDigest"):
                    return True
        return False

    def capture_orphan(self, preview):
        project = preview["sourceProjectId"]
        if not preview.get("registryRepositoryId"):
            details = self.api.request("GET", f"/projects/{project}/pipelines/{preview['sourcePipelineId']}")
            require(details["sha"] == preview["sourceSha"] and details["ref"] == preview["sourceTag"], "Orphan pipeline identity mismatch")
            require(details["status"] not in {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}, "Source pipeline is still running; retry orphan cleanup")
            # A build may publish then be cancelled before registration captured its digest.
            repositories = self.api.pages(f"/projects/{project}/registry/repositories")
            matches = [repo for repo in repositories if repo.get("location") == preview["imageRepository"]]
            if matches:
                require(len(matches) == 1, "Ambiguous owned registry repository")
                repository_id = str(matches[0]["id"])
                tag = self.api.request("GET", f"/projects/{project}/registry/repositories/{repository_id}/tags/{quoted(preview['buildImageTag'])}", absent=True)
                if tag:
                    require(bool(DIGEST.fullmatch(tag.get("digest", ""))), "Invalid orphan registry digest")
                    preview.update(registryRepositoryId=repository_id, imageDigest=tag["digest"])

    def delete_tags(self, preview):
        project = preview["sourceProjectId"]
        skipped = []
        if preview.get("imageDigest") and self.shared_digest(preview):
            return False, ["Image still referenced by another active/pending preview"]
        if preview.get("registryRepositoryId"):
            for tag_name in [preview["buildImageTag"]] + ([preview["sourceTag"]] if preview.get("sourceAliasPublished") else []):
                path = f"/projects/{project}/registry/repositories/{preview['registryRepositoryId']}/tags/{quoted(tag_name)}"
                tag = self.api.request("GET", path, absent=True)
                if tag is None:
                    continue
                if tag.get("digest") != preview["imageDigest"]:
                    skipped.append("Preserved registry tag with changed digest: " + tag_name)
                    continue
                self.api.request("DELETE", path, absent=True)
        git_path = f"/projects/{project}/repository/tags/{quoted(preview['sourceTag'])}"
        tag = self.api.request("GET", git_path, absent=True)
        if tag is not None:
            if tag["commit"]["id"] == preview["sourceSha"]:
                self.api.request("DELETE", git_path, absent=True)
            else:
                skipped.append("Preserved source tag with changed SHA")
        return True, skipped

    def advance_cleanup(self, path, record):
        preview, phase = record["preview"], record["phase"]
        if phase == "observe":
            # Prove this desired state reached ApplicationSet before asking it to disappear.
            # A 404 before first creation cannot prove the namespace was ever pruned.
            application = self.argo.application(preview["id"])
            if application is None:
                return False
            self.verify_application(application, preview)
            watched = record_path("previews", preview)
            current = self.store.read(watched)
            require(current is not None and current["status"] == "cleaning" and current["preview"] == preview, "Watched cleanup ownership changed")
            record["phase"] = "application"
            self.store.commit({watched: None, path: record}, "preview: remove observed GitOps deployment")
            return False
        elif phase == "application":
            # The foreground finalizer removes both namespace bootstrap and workloads.
            if self.argo.application(preview["id"]) is not None:
                return False
            record["phase"] = "tags"
        elif phase == "tags":
            previous_digest = preview.get("imageDigest")
            self.capture_orphan(preview)
            if preview.get("imageDigest") != previous_digest:
                self.store.commit({path: record}, "preview: persist orphan image ownership before deletion")
            finished, skipped = self.delete_tags(preview)
            record["skippedReferences"] = skipped
            if not finished:
                self.store.commit({path: record}, "preview: retain shared image cleanup")
                return False
            record["phase"] = "environment"
        elif phase == "environment":
            if preview.get("environmentId"):
                self.api.request("POST", f"/projects/{self.central_project}/environments/{preview['environmentId']}/stop", {"force": True}, absent=True)
            record.update(status="stopped", completedAt=timestamp(now()))
            # Request tombstones suppress duplicate prepare/register after Stop/expiry.
            updates = {path: None, f"archive/{preview['id']}.json": record}
            for req_path, request in self.store.records("requests"):
                if request["preview"]["id"] == preview["id"]:
                    request.update(status="stopped", buildRequired=False)
                    updates[req_path] = request
            self.store.commit(updates, "preview: finish cleanup and release hostname")
            return True
        else:
            raise Failure("Unknown cleanup phase")
        self.store.commit({path: record}, "preview: advance cleanup to " + record["phase"])
        return False

    def stop_requested(self, env):
        """Authorize a new source UI request separately from its old preview."""
        require(self.api.pipeline.get("source") == "pipeline"
                and self.api.identity.get("name") == "stop:requested"
                and env.get("PREVIEW_ACTION") == "stop",
                "Developer Stop requires the dedicated source-request Stop job")
        project = env.get("PREVIEW_SOURCE_PROJECT_ID", "")
        requesting_pipeline = env.get("PREVIEW_SOURCE_PIPELINE_ID", "")
        target_pipeline = env.get("PREVIEW_TARGET_PIPELINE_ID", "")
        for name, value in (("source project ID", project), ("requesting pipeline ID", requesting_pipeline),
                            ("target pipeline ID", target_pipeline)):
            require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value), f"Positive {name} required")
        sha = env.get("PREVIEW_SOURCE_SHA", "")
        require(isinstance(sha, str) and SHA.fullmatch(sha), "Full requesting source SHA required")
        details = self.api.request("GET", f"/projects/{project}/pipelines/{requesting_pipeline}")
        require(str(details.get("id")) == requesting_pipeline and str(details.get("project_id")) == project
                and details.get("sha") == sha, "Stop request source pipeline identity mismatch")
        require(details.get("source") == "web", "Stop requests must originate from a source New pipeline action")
        bridges = self.api.pages(f"/projects/{project}/pipelines/{requesting_pipeline}/bridges")
        require(any(item.get("name") == "stop:test"
                    and str((item.get("downstream_pipeline") or {}).get("id")) == self.central_pipeline
                    for item in bridges), "No authenticated source stop:test bridge to this central pipeline")
        actor = str(self.api.identity.get("user", {}).get("id", ""))
        require(re.fullmatch(r"[1-9][0-9]*", actor), "Authenticated Stop actor is required")
        member = self.api.request("GET", f"/projects/{project}/members/all/{actor}", absent=True)
        level = member.get("access_level") if isinstance(member, dict) else None
        require(isinstance(level, int) and level >= 30, "Developer access to the source project is required")

        # A caller supplies only an old pipeline ID. Its project is the verified
        # requesting project; the current branch/tag need not match the old build.
        target = {"PREVIEW_SOURCE_PROJECT_ID": project, "PREVIEW_SOURCE_PIPELINE_ID": target_pipeline}
        request = self.find_request(target)
        preview = request["preview"]
        require(str(preview.get("sourceProjectId")) == project, "Target preview belongs to a different source project")
        service, original_pipeline = preview.get("service"), preview.get("sourcePipelineId")
        require(isinstance(service, str) and LABEL.fullmatch(service)
                and isinstance(original_pipeline, str) and re.fullmatch(r"[1-9][0-9]*", original_pipeline),
                "Stored preview identity is invalid")
        expected_id = f"{service}-{project}-{original_pipeline}"
        require(preview.get("id") == expected_id and preview.get("namespace") == "test-" + expected_id,
                "Stored preview project/namespace identity mismatch")
        require(request.get("status") in {"pending", "provisioning", "active", "cleaning", "stopped"},
                "Target preview request has an invalid status")
        if request["status"] == "stopped":
            # An old request is an idempotent no-op, never a lookup by a reused
            # environment name that could delete a newer preview.
            return request
        self.stop(target)
        result = self.find_request(target)
        require(result.get("status") in {"cleaning", "stopped"}, "Target preview has no tracked cleanup state")
        return result

    def stop(self, env):
        request = self.find_request(env)
        require(request["status"] != "stopped", "This request is already stopped; it cannot stop a later lease reusing the environment")
        preview = request["preview"]
        path = record_path("cleanup", preview)
        record = self.store.read(path)
        if record is None:
            for directory in ("previews", "pending"):
                candidate_path = record_path(directory, preview)
                candidate = self.store.read(candidate_path)
                if candidate:
                    path, record = self.begin_cleanup(candidate_path, candidate, "manual stop")
                    break
        if record:
            # Bounded phases; scheduled reaping resumes when Argo/namespace deletion is asynchronous.
            for _ in range(4):
                before = record["phase"]
                if self.advance_cleanup(path, record):
                    return
                if record["phase"] == before:
                    return

    def reap(self):
        failures = []
        for directory in ("pending", "previews"):
            for path, record in self.store.records(directory):
                if record["status"] == "cleaning":
                    continue
                setup = record["status"] in {"pending", "provisioning"}
                expiry = record["preview"]["setupDeadline" if setup else "expiresAt"]
                if instant(expiry) <= now():
                    try:
                        self.begin_cleanup(path, record, "setup timeout" if setup else "lease expiry")
                    except Failure as error:
                        failures.append(str(error))
        for path, record in self.store.records("cleanup"):
            try:
                for _ in range(4):
                    before = record["phase"]
                    if self.advance_cleanup(path, record) or record["phase"] == before:
                        break
                if (record["phase"] in {"observe", "application"}
                        and instant(record["cleanupStartedAt"]) + dt.timedelta(minutes=15) <= now()):
                    failures.append(f"{record['preview']['id']}: overdue {record['phase']} deletion; inspect finalizers and controller health")
            except Failure as error:
                failures.append(f"{record['preview']['id']}: {error}")
        try:
            self.reap_unregistered_images()
        except Failure as error:
            failures.append(str(error))
        require(not failures, "Cleanup requires retry: " + "; ".join(failures))

    def reap_unregistered_images(self):
        """Find immutable build tags left by cancelled pipelines that never prepared state."""
        records = self.all_records()
        reserved = {(item["preview"]["sourceProjectId"], item["preview"]["buildImageTag"]) for _, item in records}
        for path in (ROOT / "catalog").glob("*.json"):
            catalog = json.loads(path.read_text())
            if not catalog.get("enabled"):
                continue
            project = str(catalog["source"]["projectId"])
            repositories = self.api.pages(f"/projects/{project}/registry/repositories")
            for repository in repositories:
                if repository.get("location") != catalog["image"]["repository"]:
                    continue
                for summary in self.api.pages(f"/projects/{project}/registry/repositories/{repository['id']}/tags"):
                    match = re.fullmatch(r"preview-([0-9]+)-([0-9]+)", summary["name"])
                    if not match or match[1] != project or (project, summary["name"]) in reserved:
                        continue
                    pipeline = self.api.request("GET", f"/projects/{project}/pipelines/{match[2]}", absent=True)
                    if not pipeline or pipeline["status"] in {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}:
                        continue
                    if not TAG.fullmatch(pipeline["ref"]) or instant(pipeline["created_at"]) + dt.timedelta(hours=12) > now():
                        continue
                    tag_path = f"/projects/{project}/registry/repositories/{repository['id']}/tags/{quoted(summary['name'])}"
                    tag = self.api.request("GET", tag_path, absent=True)
                    if not tag or not DIGEST.fullmatch(tag.get("digest", "")):
                        continue
                    preview = {"id": "orphan-" + project + "-" + match[2], "imageRepository": catalog["image"]["repository"], "imageDigest": tag["digest"]}
                    if not self.shared_digest(preview):
                        self.api.request("DELETE", tag_path, absent=True)


def write_result(request):
    preview = request["preview"]
    output = {"id": preview["id"], "namespace": preview["namespace"], "status": request["status"], "expiresAt": preview.get("expiresAt"), "url": "https://" + preview["host"] + "/" if preview.get("host") else None}
    print(json.dumps(output))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "register", "ready", "activate", "stop", "stop-request", "reap-kubernetes"])
    action = parser.parse_args().action
    api = GitLab(os.environ.get("PREVIEW_GITLAB_TOKEN") or os.environ.get("HELM_TAG_TOKEN"), os.environ.get("CI_JOB_TOKEN"))
    if action == "reap-kubernetes":
        project, pipeline = api.cluster_reaper_identity()
        Lifecycle(api, Store(api, project), KubernetesArgo(), project, pipeline).reap()
        return
    project, pipeline = api.current_identity()
    observer = ArgoCD(os.environ.get("ARGOCD_SERVER", ARGOCD_SERVER), os.environ.get("ARGOCD_AUTH_TOKEN"))
    lifecycle = Lifecycle(api, Store(api, project), observer, project, pipeline)
    if action == "stop-request":
        result = lifecycle.stop_requested(os.environ)
        write_result(result)
        if result["status"] == "cleaning":
            print("Cleanup requested. Argo and the scheduled reaper finish teardown; capacity returns after confirmed namespace deletion.")
        else:
            print("Preview is already stopped; no newer lease was changed.")
        return
    pipeline_source = api.pipeline["source"]
    if action in {"prepare", "register", "ready", "activate"}:
        require(pipeline_source == "pipeline", "Registration requires a verified multi-project pipeline")
    elif action == "stop":
        require(api.identity["name"] in {"stop", "stop:standalone"}, "Stop must execute through the trusted manual Stop job")
        if pipeline_source == "pipeline":
            require(os.environ.get("PREVIEW_ACTION") == "register", "Only a registration pipeline may expose manual Stop")
            lifecycle.verified_source(os.environ, verify_tag=False)
        else:
            require(pipeline_source == "web", "Standalone Stop requires a manual central pipeline")
            member = api.request("GET", f"/projects/{project}/members/all/{api.identity['user']['id']}")
            require(member.get("access_level", 0) >= 40, "Standalone Stop requires a central maintainer")
    if action in {"prepare", "register", "ready", "activate"}:
        write_result(getattr(lifecycle, action)(os.environ))
    elif action == "stop":
        lifecycle.stop(os.environ)


if __name__ == "__main__":
    try:
        main()
    except (Failure, KeyError, ValueError, OSError) as error:
        # Library exceptions can contain credential-bearing request details.
        message = str(error) if isinstance(error, Failure) else type(error).__name__
        print(f"Preview operation failed: {message}", file=sys.stderr)
        sys.exit(1)
