local metadata = obj.metadata or {}
local labels = metadata.labels or {}
local hs = {status = "Healthy", message = "ConfigMap available"}
if metadata.name ~= "preview-registry-ready" then
  return hs
end
local namespace = metadata.namespace or ""
local pilotNamespace = string.match(namespace, "^test%-example%-app%-100%-[1-9][0-9]*$") ~= nil
if not pilotNamespace and labels["company.example/registry-gate"] ~= "true" then
  return hs
end
hs.status = "Degraded"
hs.message = "Invalid preview registry readiness gate"
local id = labels["company.example/preview-id"]
if not pilotNamespace or type(id) ~= "string" or namespace ~= "test-" .. id or
   labels["company.example/source-project-id"] ~= "100" or labels["company.example/ephemeral"] ~= "true" or
   labels["company.example/registry-gate"] ~= "true" then
  return hs
end
local allowed = {
  ["company.example/preview-id"] = true, ["company.example/source-project-id"] = true,
  ["company.example/ephemeral"] = true, ["company.example/registry-gate"] = true,
  ["app.kubernetes.io/part-of"] = true, ["app.kubernetes.io/instance"] = true
}
for key, value in pairs(labels) do
  if not allowed[key] then return hs end
end
if (labels["app.kubernetes.io/part-of"] ~= nil and labels["app.kubernetes.io/part-of"] ~= "preview-platform") or
   (labels["app.kubernetes.io/instance"] ~= nil and labels["app.kubernetes.io/instance"] ~= id) or
   metadata.deletionTimestamp ~= nil or obj.immutable == true then
  return hs
end
for key, value in pairs(metadata.ownerReferences or {}) do return hs end
for key, value in pairs(metadata.finalizers or {}) do return hs end
for key, value in pairs(obj.binaryData or {}) do return hs end
local data = obj.data or {}
local count = 0
for key, value in pairs(data) do
  count = count + 1
  if key ~= "state" and key ~= "sourceResourceVersion" then return hs end
end
if count ~= 2 or type(data.sourceResourceVersion) ~= "string" or
   (data.state ~= "Pending" and data.state ~= "Ready") then
  return hs
end
if data.state == "Ready" and data.sourceResourceVersion ~= "" then
  hs.status = "Healthy"
  hs.message = "Preview registry credential is synchronized"
else
  hs.status = "Progressing"
  hs.message = "Waiting for the preview registry credential"
end
return hs
