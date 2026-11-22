# Preview namespace chart

Creates the explicitly tracked Namespace at wave -30, approved quota, limits,
network policies and registry-controller RoleBinding at wave -20, and the registry
readiness gate at wave -10. The workload chart starts at wave 0.

Defaults are deliberately incomplete. Render a synthetic, credential-free example:

```sh
helm lint charts/previews -f charts/previews/examples/example-app.yaml
helm template preview charts/previews -f charts/previews/examples/example-app.yaml
```

The lifecycle supplies identity, provisioning resources, required Secret names and
expiry from a pinned approved catalog. The chart never embeds Secret values.
The registry controller and Argo ConfigMap health customization are required.
See the repository installation guide for policies and bootstrap ordering.
