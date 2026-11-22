# Operations and live acceptance

Run this sequence on infrastructure you control before allowing general use.
Offline validation cannot establish image-pull access, admission enforcement,
network isolation, wildcard TLS or actual finalizer deletion.

## First preview

1. Keep deployment switches disabled until both controllers are healthy and a
   reaper Job succeeds with the configured GitLab token.
2. Approve the catalog and enable central `PREVIEW_PLATFORM_ENABLED=true` and
   source `TEST_DEPLOYMENTS_ENABLED=true`. Create a fresh `test-demo` application tag.
3. Follow the source prepare, build, deploy and report jobs. Confirm the build's
   artifact digest matches the registry and the generated Application's exact two
   source revisions. Readiness must see Synced/Healthy and the registry gate Ready.
4. Visit `/`, `/health` and `/healthz` over the generated HTTPS host. Confirm one
   non-root Pod, token-free workload ServiceAccount, matching namespace ownership,
   quota, default-deny policy and approved ingress/DNS policies.
5. Verify DNS works, ingress reaches the app, and unapproved cross-namespace and
   external network traffic is denied by the actual CNI.
6. Retry the pipeline. An active identical tag/SHA must reuse the digest and original
   expiry; it must not renew its 12-hour lease or consume another slot.

## Cleanup and failure cases

- Request early Stop through a source web pipeline using the original source
  pipeline ID. Verify no build runs. A source Developer+ can stop any preview in
  that same source project; a different project or invalid bridge must be rejected.
- Wait for Application deletion, finalizer completion and Namespace absence.
  Only then should owned build-image and test-Git tags disappear and capacity return.
  Check `cleanup/`, `archive/` and `requests/` state, not just the GitLab environment badge.
- Let a second preview expire and observe the five-minute CronJob dispatch and
  asynchronous teardown. Verify a setup failure is also cleaned after its
  30-minute setup deadline.
- Try four distinct preview requests. The fourth must fail before build until
  one of the first three has completed namespace teardown. Check stale Stop and
  retry requests cannot remove a newer preview.
- Restart/retry interrupted registration, readiness and cleanup. API failures,
  delayed ApplicationSet generation or stuck finalizers must retain ownership
  records for retry. Do not manually delete reservation records to bypass a failure.
- Rotate the source pull credential in the permanent namespace and verify the
  controller updates owned target Secrets and gate versions. It must refuse to
  adopt an existing foreign or Argo-managed Secret.

The name uses only the first three source-SHA characters. Two different requests
with the same hostname collide safely and are rejected; use the existing preview
or wait for its cleanup. Multi-service adapters and alternative naming rules need
coordinated changes to validation, charts, policies and tests.

## Pause and recover

Set source `TEST_DEPLOYMENTS_ENABLED=false` to stop new previews while keeping the
source Stop-only pipeline available. Leave central enabled if that Stop bridge
must work. Do not disable the reaper until existing previews are drained. A
central Maintainer may also run a central web pipeline with `PREVIEW_ACTION=stop`,
`PREVIEW_SOURCE_PROJECT_ID` and `PREVIEW_SOURCE_PIPELINE_ID`, then play
`stop:standalone`.

Check controller logs and Argo Application conditions without dumping Secrets or
CI environment variables. Repair API permissions, unreachable chart commits,
registry access or finalizers, then allow the serialized cleanup flow to retry.
The reaper uses the current protected `main` revision and refuses to act if that
revision changes during startup; the next sweep retries.
