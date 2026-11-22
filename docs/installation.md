# Installation

The platform repository is public source. Runtime deployment still uses GitLab
CI, its registry and API. Keep your configured platform/state repository private
if source commit, tag, user or environment metadata should not be public.

## 1. Create two GitLab projects

Import this repository into a platform project, illustrated as `company/previews`
(ID `200`). Copy the **contents** of `examples/example-app/`, including dotfiles,
into a separate application project, illustrated as `company/example-app` (ID
`100`). Give each project a `main` branch. Keep the platform CI configuration at
`.gitlab-ci.yml`; the application uses its copied `.gitlab-ci.yml`.

Protect platform `main`, restrict direct pushes to trusted automation and
Maintainers, disable force push, and review changes before merge. Source developers
must be able to run the protected central downstream pipeline. Configure protected
branch permissions and the minimum role for pipeline-variable overrides for the
roles you explicitly trust; the supplied Stop flow expects source Developers to
be able to run the central pipeline. A Developer allowed to merge platform code
is also trusted with that platform's automation. Use an appropriate approval
policy for this boundary.

In the central project's **CI/CD job token allowlist**, allow the application
project to read request files and trigger downstream pipelines. The initiating
user also needs the relevant project permissions. In the source project, permit
central reads where job-token authentication is used by your GitLab setup; the
central API client normally reads source metadata and artifacts with its dedicated
API token. See [job-token access](https://docs.gitlab.com/ci/jobs/ci_job_token/)
and [downstream permissions](https://docs.gitlab.com/ci/pipelines/downstream_pipelines/).

## 2. Configure the trusted repository

Commit all site-specific changes through your trusted review process:

| File | Set for your installation |
| --- | --- |
| `config/platform.json` | GitLab HTTPS API URL, central path/ID, `main`, repository URL, both chart paths, Argo CD HTTPS endpoint and preview domain |
| `catalog/example-app.json` | Source project ID/path, exact registry image path, chart/provisioning repositories and immutable 40-character revisions |
| `charts/example-app/values-test.yaml` | Same registry image repository as the catalog |
| `charts/example-app/values.yaml` | Public source URL, ingress class and any TLS configuration |
| `examples/example-app/.gitlab-ci.yml` | Fixed trigger project, matching the central project path |
| `platform/bootstrap/applications/main.yaml` | State and chart repository URLs, chart paths and state revision |
| `platform/bootstrap/project/preview.yaml` | The same approved repository URL and preview namespace prefix |
| `platform/bootstrap/policies/registry-sync.yaml` | Replace example source ID `100` in every CEL validation |
| `platform/registry-sync/render_manifests.py` | The same source ID in `SOURCE_PROJECT_ID` |
| `platform/registry-sync/gate-health.lua` | The same source ID in both namespace and ownership checks |
| `provisioning/example-app-network.json` | Actual ingress controller namespace and DNS selectors |

`company.example/` is a fixed ownership-label prefix shared by the code, charts,
Lua and admission policies. It may be retained as the example protocol prefix. If
you rename it, replace it consistently across the entire repository and rerun all
tests. Keep `example-app` as the supported service key for this sample; adapting
another service requires a reviewed adapter and matching identity checks.

The GitOps repository in this example contains both charts. After publishing a
reviewed chart commit, set `chart.approvedRevision` and
`provisioning.approvedRevision` to reachable full commits in that repository.
Zero SHA values are placeholders. The pins can be the same commit. Later catalog
approval changes belong in a subsequent commit, avoiding a self-referential SHA.
Do not rebase away chart revisions referenced by live previews.

Initially keep `enabled`, `provisioning.approved` and `dependenciesReviewed` false.
Review the source, chart, network access, quotas and required Secret names before
setting these three booleans true. Keep platform and source deployment switches
disabled until controllers and cleanup are ready.

## 3. Build the trusted tools image

The tools image contains Python, Docker CLI, the source helper, the reaper
bootstrap and the reviewed `config/platform.json`. Build from the repository root
using a reviewed **Python Alpine** base image by digest:

```sh
docker build --build-arg BASE_IMAGE="$PYTHON_ALPINE_IMAGE"   -f tools/Dockerfile -t "$TOOLS_IMAGE_TAG" .
docker push "$TOOLS_IMAGE_TAG"
```

These shell variables are operator inputs. For example, `PYTHON_ALPINE_IMAGE`
should be `python:3.13-alpine@sha256:<actual digest>` and `TOOLS_IMAGE_TAG` should
point to a repository you control. Record the resulting immutable image digest.
Rebuild the tools image whenever its trusted scripts or platform config change.
The `apk` packages are resolved at build time; keep the reviewed built image pinned
and rebuild it through your normal patch process.

Set its immutable image in:

- Central GitLab `PREVIEW_TOOLS_IMAGE`.
- Source GitLab `TEST_PREVIEW_CI_IMAGE`.
- `tools/image-lock.json`, then regenerate registry controller manifests.
- `platform/bootstrap/controllers/cluster-reaper.yaml`.

```sh
python3 platform/registry-sync/render_manifests.py   --output platform/bootstrap/controllers/registry-sync.yaml
```

The checked-in zero digest is intentionally undeployable. The source build runner
also needs `TEST_DIND_IMAGE` set to an approved `docker:<version>-dind@sha256:...`
compatible with the CLI in the tools image. Use a dedicated, ephemeral privileged
runner for that job with a shared client-cert volume, for example the runner's
`[runners.docker]` settings `privileged = true` and
`volumes = ["/certs/client", "/cache"]`. Do not share its Docker daemon with
central deployment jobs. The sample pipeline configures TLS on port 2376.

## 4. Configure credentials outside Git

Create all real secrets in GitLab settings, a secret manager or Kubernetes. Never
paste token values into the catalog, chart values or Git manifests.

| Location | Variable or Secret | Requirement |
| --- | --- | --- |
| Central GitLab | `PREVIEW_GITLAB_TOKEN` | Dedicated API token with `api` scope, central Maintainer access and permission to write protected `main`; source metadata/artifact/registry/tag permissions sufficient for the lifecycle |
| Central GitLab | `ARGOCD_AUTH_TOKEN` | Read-only Application GET in `previews/*`; no sync/delete permission is needed by CI |
| Central GitLab | `PREVIEW_TOOLS_IMAGE` | Approved tools image pinned by SHA-256 digest |
| Central GitLab | `PREVIEW_PLATFORM_ENABLED` | Protected variable; set `true` only after acceptance prerequisites |
| Source GitLab | `TEST_PREVIEW_CI_IMAGE`, `TEST_DIND_IMAGE` | Reviewed digest-pinned images accessible to the runner |
| Source GitLab | `TEST_DEPLOYMENTS_ENABLED` | Unprotected `true` for unprotected test-tag pipelines; default is `false` |
| Kubernetes `previews` | `company-registry` | Docker pull Secret for the source app registry; read-only pull credential |
| Kubernetes `argocd` | `preview-gitlab-api`, key `token` | Reaper API token with the required central/source lifecycle permissions |
| Kubernetes `argocd` | `company-registry` | Pull access to the tools image if it is private |
| Argo CD | Project-scoped Git repository credential | Read access to the configured platform/state/chart repository |

Central sensitive CI variables must be protected and masked/hidden where supported,
with expansion disabled and environment scope `*`. Do not set `ARGOCD_SERVER` as a
conflicting variable: the trusted config supplies the approved endpoint. Never
make central API, Argo or cluster tokens available in the source project. Avoid
inherited group/instance variables that expose them to application jobs. The
source job additionally removes legacy infrastructure-token variables before
calling the helper; that removal does not replace correct variable scoping.

For Kubernetes, use a secret manager or create a Docker Secret from a temporary
Docker config file and a token Secret from a token file with restrictive local
permissions. Example commands reference files, not credential literals:

```sh
kubectl -n previews create secret generic company-registry   --type=kubernetes.io/dockerconfigjson   --from-file=.dockerconfigjson=/secure/path/docker-config.json
kubectl -n argocd create secret generic preview-gitlab-api   --from-file=token=/secure/path/gitlab-token
```

Use a pull credential scoped to the sample image. `previews/company-registry`
is copied into owned sample namespaces, so every preview workload in scope can
consume it. Do not use an administrator or write credential as a pull Secret.

## 5. Bootstrap the cluster

Use Kubernetes 1.30+ with `admissionregistration.k8s.io/v1` policies, Argo CD with
ApplicationSet and multi-source support, an ingress controller and a CNI that
enforces both ingress and egress NetworkPolicy. Verify your actual API versions
and controller behavior. `external-secrets.io` entries are optional allowlist
entries; the sample needs no External Secrets operator.

1. Register the configured Git repository with Argo CD for project `previews`.
   Ensure the ApplicationSet controller can read it and the Argo application
   controller can manage the allowed resources, including Namespaces.
2. Apply `platform/bootstrap/namespaces/previews.yaml`, then create the out-of-band
   Secrets above, including the `argocd/company-registry` pull Secret if needed.
3. Apply `platform/bootstrap/policies/` **before** starting the registry controller.
   Check policy status for type-checking warnings and verify the bindings enforce
   Deny. The controller can create Secrets in namespaces granting its target role;
   admission is part of the security boundary.
4. Apply `platform/bootstrap/project/preview.yaml` and
   `platform/bootstrap/controllers/`. Check the registry Deployment and reaper
   CronJob become healthy. These controllers use projected ServiceAccount tokens
   and narrow RBAC; no static Kubernetes token file is included.
5. Merge the contents of `platform/registry-sync/gate-health.lua` into the existing
   Argo CD `argocd-cm` key `resource.customizations.health.ConfigMap`. Preserve
   other settings. One way to patch only that key is:

   ```sh
   python3 - <<'PY' > /tmp/preview-gate-patch.json
   import json
   from pathlib import Path
   print(json.dumps({"data": {"resource.customizations.health.ConfigMap":
                            Path("platform/registry-sync/gate-health.lua").read_text()}}))
   PY
   kubectl -n argocd patch configmap argocd-cm --type merge      --patch-file /tmp/preview-gate-patch.json
   ```

   If an existing ConfigMap health customization exists, combine both scripts
   through your reviewed Argo configuration instead of overwriting it. The preview
   gate waits at sync wave `-10`; the workload is wave `0`. Without this health
   customization, bootstrap ordering and central readiness are incomplete.
6. Apply `platform/bootstrap/applications/main.yaml`. Its file generator watches
   `previews/example-app/*.yaml`. Keep `requests/`, `pending/` and `cleanup/`
   outside that watched path. Never add `CreateNamespace=true`: the chart explicitly
   tracks the Namespace and ownership labels for finalizer cleanup.
7. Configure wildcard DNS `*.your-preview-domain` to the ingress controller and a
   matching HTTPS certificate. The sample Ingress uses your controller's default
   certificate unless you add reviewed per-namespace TLS delivery. Check that the
   ingress namespace can reach port 8080 and the DNS selectors match your cluster.
8. Confirm a successful reaper Job, registry-controller health, Argo Git access,
   policy enforcement and offline checks. Approve the catalog and enable the
   central/source switches. Run the live acceptance steps in [operations](operations.md).

These commands apply platform files only. Do not recursively apply the entire
repository: it also contains test fixtures, charts and examples.

For details on generated Applications and deletion, see the official
[Git generator](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Generators-Git/),
[ApplicationSet deletion](https://argo-cd.readthedocs.io/en/stable/operator-manual/applicationset/Application-Deletion/)
and [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)
documentation.
