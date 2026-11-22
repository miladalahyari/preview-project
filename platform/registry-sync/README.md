# Registry Secret delivery

`registry_sync.py` copies only `previews/company-registry` into nonterminating,
owned `test-example-app-<source-project>-<pipeline>` namespaces. It preserves target
ownership, refuses foreign/adopted resources, and updates the `preview-registry-ready`
ConfigMap data after successful delivery. Projected ServiceAccount credentials are
used only inside Kubernetes. No credential value is included in source control.

Set the approved source project ID in the renderer, Lua and admission policies,
set the tools image in `tools/image-lock.json`, and regenerate the Deployment/RBAC:

```sh
python3 platform/registry-sync/render_manifests.py   --output platform/bootstrap/controllers/registry-sync.yaml
```

Install the admission policies first. Merge `gate-health.lua` into Argo CD's
`resource.customizations.health.ConfigMap` setting. The ApplicationSet ignores
only gate `/data`; the synchronizer cannot change Argo-owned metadata.
Run `python3 -B -m unittest discover -s platform/registry-sync/tests` for controller
and local Lua tests. Live admission, Argo health, image pull and rotation checks
are still required.
