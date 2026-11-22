# Verification report

Checked on **2026-11-22**. This describes local preparation of the public example;
it is not a claim of a successful GitLab or Kubernetes rollout.

| Check | Result |
| --- | --- |
| Offline Python tests | 182 passed across lifecycle/authorization, distribution wiring, source build, registry sync, Lua health, both charts and sample HTTP response behavior |
| Helm chart lint/render | Both charts passed using Helm 3.22.1; incomplete namespace defaults and missing/mutable workload digests fail as intended |
| JSON/YAML validation | All source configuration parsed; duplicate YAML keys rejected |
| Generated registry controller | Matches the checked-in controller source and configured tools-image reference |
| Local HTTP smoke test | `/`, `/health`, `/healthz`, query strings and unknown-route 404 passed against a running server |
| Public-name scan | No previous application/company names or private local paths remain in publishable files |
| Gitleaks 8.30.1 directory scan | No findings, including decode depth 3; scanned uncommitted source too |
| Gitleaks 8.30.1 history scan | No findings in all reachable history, including decode depth 3; one existing commit scanned |
| Container build | Attempted; local Docker daemon could not pull `python:3.13-alpine` and returned a registry RBAC access denial. No container runtime acceptance is claimed |
| Remote GitLab CI Lint/pipeline execution | Not run; no configured public test installation was provided. YAML and lifecycle wiring were checked locally |
| Live Kubernetes/Argo acceptance | Not run; use the operations checklist after configuring your own infrastructure |

The supplied reference infrastructure contained nonempty encoded Secret values.
Those manifests were excluded from the public project. Source directories outside
this repository were inspected without modification. No live credential was copied,
printed, revoked or rotated.

Existing operational request/archive records and private rollout notes were
removed from the public working tree. Only empty state directories remain.
Catalog deployment approvals are false and chart/image revision placeholders must
be replaced before enabling the platform. The existing AGPL v3 license is retained.

Secret scanning is evidence about the checked files and history, not a guarantee
about external credential stores or future changes. Repeat both scans immediately
before publication. No commit, push, pull request or live deployment was performed.
