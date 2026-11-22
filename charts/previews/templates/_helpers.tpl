{{/* This pilot deliberately supports only the approved example-app identity. */}}
{{- define "preview-namespace.validate" -}}
{{- $preview := .Values.preview -}}
{{- if ne .Values.provisioning.registrySecretDelivery true -}}
  {{- fail "provisioning.registrySecretDelivery must be true for the approved registry integration" -}}
{{- end -}}
{{- if not (regexMatch "^example-app-[1-9][0-9]*-[1-9][0-9]*$" $preview.id) -}}
  {{- fail "preview.id must be example-app-<positive project ID>-<positive source pipeline ID>" -}}
{{- end -}}
{{- if not (hasPrefix (printf "example-app-%s-" $preview.sourceProjectId) $preview.id) -}}
  {{- fail "preview.id must contain the exact sourceProjectId" -}}
{{- end -}}
{{- if ne $preview.namespace (printf "test-%s" $preview.id) -}}
  {{- fail "preview.namespace must equal test-<preview.id>" -}}
{{- end -}}
{{- if gt (len $preview.namespace) 63 -}}
  {{- fail "preview.namespace must fit one 63-character DNS label" -}}
{{- end -}}
{{- if ne $preview.expiresAt "" -}}
{{- $parsedExpiry := mustToDate "2006-01-02T15:04:05Z" $preview.expiresAt -}}
{{- end -}}
{{- range $name := .Values.provisioning.requiredSecrets -}}
  {{- range $part := splitList "." $name -}}
    {{- if gt (len $part) 63 -}}{{- fail "requiredSecrets names must use DNS labels of at most 63 characters" -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- $seen := dict -}}
{{- range $index, $resource := .Values.provisioning.resources -}}
  {{- $name := $resource.metadata.name -}}
  {{- range $part := splitList "." $name -}}
    {{- if gt (len $part) 63 -}}{{- fail "provisioning resource names must use DNS labels of at most 63 characters" -}}{{- end -}}
  {{- end -}}
  {{- if has $name (list "preview-quota" "preview-limits" "default-deny" "preview-registry-ready" "preview-registry-sync" "default" $preview.id) -}}
    {{- fail (printf "provisioning.resources[%d] uses a reserved resource name" $index) -}}
  {{- end -}}
  {{- if and (hasKey $resource.metadata "namespace") (ne $resource.metadata.namespace $preview.namespace) -}}
    {{- fail (printf "provisioning.resources[%d] has a foreign namespace" $index) -}}
  {{- end -}}
  {{- $identity := printf "%s/%s" $resource.kind $name -}}
  {{- if hasKey $seen $identity -}}
    {{- fail (printf "duplicate provisioning resource: %s" $identity) -}}
  {{- end -}}
  {{- $_ := set $seen $identity true -}}
  {{- range $key, $value := ($resource.metadata.annotations | default dict) -}}
    {{- if or (hasPrefix "argocd.argoproj.io/" $key) (hasPrefix "company.example/" $key) -}}
      {{- fail (printf "provisioning.resources[%d] cannot override ownership or Argo CD annotations" $index) -}}
    {{- end -}}
  {{- end -}}
  {{- range $key, $value := ($resource.metadata.labels | default dict) -}}
    {{- if or (hasPrefix "argocd.argoproj.io/" $key) (hasPrefix "company.example/" $key) (eq $key "app.kubernetes.io/instance") -}}
      {{- fail (printf "provisioning.resources[%d] cannot override ownership labels" $index) -}}
    {{- end -}}
  {{- end -}}
  {{- if eq $resource.kind "ExternalSecret" -}}
    {{- $target := $resource.spec.target | default dict -}}
    {{- $template := $target.template | default dict -}}
    {{- if or (hasKey $template "data") (hasKey $template "stringData") -}}
      {{- fail "ExternalSecret inline template data is not accepted; use reviewed remote references" -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- define "preview-namespace.labels" -}}
app.kubernetes.io/part-of: preview-platform
company.example/ephemeral: "true"
company.example/preview-id: {{ .Values.preview.id | quote }}
company.example/source-project-id: {{ .Values.preview.sourceProjectId | quote }}
{{- end -}}
