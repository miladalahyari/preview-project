# Security and publication

Store real credentials only in GitLab's protected CI settings, Kubernetes Secrets
or your secret manager. The repository contains Secret **names** and synthetic
unit-test values; these are not deployable credentials. Base64 encoding does not
make a Kubernetes Secret safe to publish.

Central configuration and the catalog are trusted code. Source requests cannot
select a central repository, arbitrary image digest, chart path or Kubernetes
namespace. The lifecycle validates real GitLab job identity, source bridge,
project mapping, artifact and registry metadata before publishing GitOps state.
Argo sources and ownership must match exactly before readiness or deletion.

Preview workloads receive only a pull credential scoped for the sample registry
image, not central API, Argo or Kubernetes administrator credentials. Review
GitLab variable inheritance and runner separation in the installation guide.
The namespace chart rejects inline Secrets and unreviewed resource types.

## Repeat the publication check

Install [Gitleaks](https://github.com/gitleaks/gitleaks) through your normal verified
software process, then scan both files and all reachable Git history:

```sh
gitleaks dir . --redact --max-decode-depth 3
gitleaks git . --log-opts=--all --redact --max-decode-depth 3
```

The directory scan includes uncommitted files; the history scan includes committed
content. Keep scan reports outside the repository. Review ignored/local files and
archives before creating release bundles. `.gitignore` helps avoid accidents but
does not protect previously committed credentials.

If a real credential is found, revoke/rotate it and remove its value before
publication. Removing a current file does not remove it from Git history. Do not
publish local GitButler operation logs, `.git/`, `.env`, Docker configs or kubeconfigs.

## Reporting

Report sensitive issues privately to the repository maintainer through the hosting
platform's private vulnerability reporting feature when enabled. Include affected
versions and reproduction steps; omit live credential values from public issues.
