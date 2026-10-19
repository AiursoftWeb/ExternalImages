# GitLab Pipeline AlertCenter Bridge — proposed design

Status: implementation merged; configurable endpoints and automatic GitLab token rotation are being prepared for deployment.

Run one continuously running container, `gitlab_pipeline_alert_center_bridge`,
from ExternalImages. Observe company default-branch pipelines through GitLab's
read-only API and report prolonged failures to AlertCenter. Monitored repositories
need no CI changes, includes, installed tools, webhooks, or credentials.

## Scope and configuration

- Required scope: `GITLAB_GROUPS`, a comma-separated set of GitLab group paths.
  Production sets `GITLAB_GROUPS=aiursoft`. Trim whitespace, remove duplicates,
  and resolve each exact group path; do not use substring or prefix matching.
  Missing/empty configuration or an invalid/inaccessible group fails validation;
  never fall back to every project visible to the token.
- Discover active projects owned by the configured groups and their subgroups;
  deduplicate projects when configured groups overlap. Group ownership, not token
  visibility, defines the monitoring boundary.
  Exclude shared-in projects, personal namespaces (including `anduin`), bot forks,
  archived projects, tags, MR pipelines, child pipelines, and non-default branches.
  Verify ownership independently of what the token can access. With production
  scope `aiursoft`, no other organization is monitored.
- Monitor the overall top-level pipeline result, including build, test and deploy
  failures. Allowed failures whose overall pipeline succeeds do not start alerts.
- Required secrets: `GITLAB_ACCESS_TOKEN` (dedicated personal token with `read_api` and `self_rotate`) and
  `ALERTCENTER_API_KEY` (one dedicated application for the entire bridge).
- Required endpoints: `GITLAB_URL` and `ALERTCENTER_URL`; no company-specific defaults.
  Do not reuse the revoked CI reporting key.
- No UI, incoming webhook, database, or per-repository configuration.
  A writable `/data` volume stores the rotated GitLab credential only.
  Proposed initial intervals: pipeline scan and scope discovery every 60 seconds; thresholds 3 and 8 hours. Keep these as simple service defaults.

## Failure-duration policy

Each project/default-branch pair has an independent failure episode.

| Observation | Action |
| --- | --- |
| Current failure episode | Derive its start from GitLab history; compare its age with the thresholds |
| Still failing for less than 3 hours | Keep observing |
| Still failing for at least 3 hours | Create one Sev3 incident |
| Still failing for at least 8 hours | Escalate that incident to Sev2 |
| Newer successful pipeline | End the failure episode; mitigate the existing incident, or skip if none |
| Running, pending, manual, canceled or skipped pipeline | Neither start a failure nor declare recovery |

A single failed pipeline left unfixed crosses the thresholds without another
build. Further failures do not restart the clock. New builds in progress do not
reset an existing failure episode. Recovery means a successful pipeline, not
merely a new commit or cancellation. Threshold actions occur on the next healthy
scan, not at an exact wall-clock deadline. Preserve manually assigned Sev0/Sev1.

## GitLab is the source of truth

On every scan, read the default branch's relevant pipeline history, paginated in
pipeline ID order. Consider successful and failed top-level pipelines; the other
statuses do not establish recovery. The newest relevant result determines whether
the branch is healthy. An older pipeline completing late cannot override a newer
successful pipeline.

If failing, walk backward to the most recent successful pipeline (or the end of
available history). The failures after that boundary form the current episode.
Derive its start from the earliest failure completion timestamp in that episode,
using pipeline details (`finished_at`), not container startup, first observation,
pipeline creation, or the last successful build time. Compute age as UTC now minus
that timestamp. Revalidate the relevant results before writing an alert.

Example: success at 09:00, first failure at 10:00, another failure at 11:00.
The failure age starts at 10:00: Sev3 at 13:00, Sev2 at 18:00. A container restart
at 12:00 changes neither deadline. Starting the service for the first time at
19:00 creates Sev2 directly if the branch is still failing; do not emit a redundant
Sev3 notification first. A later success ends the episode; the next failure starts
a new one.

Re-read pipeline statuses and timestamps, including same-pipeline retries: an ID
is not an immutable status event. A failed pipeline retried successfully must
recover without requiring a new pipeline ID. Recompute from GitLab's currently
retained records rather than inventing an event history. Deleted pipelines and
historical statuses overwritten by retries cannot be reconstructed by the list
API; missing timestamps or incomplete requests must not produce a guessed age.
The acceptance stage must cover retry history explicitly against the deployed
GitLab version. Cache data must never override fresh GitLab evidence.

## Memory and restart behavior

Memory is disposable cache only: project discovery, fetched pipeline details and
incident references. No failure timer, checkpoint or persisted queue is required
for correctness. A cold start performs the same history scan as a warm process;
restarts and downtime do not reset failure age. Run one replica with
stop-before-start replacement to keep reporting simple.

Query AlertCenter by exact stable bridge identity (source, GitLab instance,
project ID, branch) and reconcile the desired state computed from GitLab with the
existing incident. A healthy project mitigates its incident; a failing project
creates or updates it only when the applicable duration threshold is met. Never
adopt legacy CI incidents or unrelated application incidents. AlertCenter metadata
is reporting context, not the authoritative clock or pipeline history.

Store only reporting context in incident metadata: source/schema version,
project/branch identity, failure start UTC, and latest relevant pipeline ID/URL.
Use the existing SDK, exact query and versioned metadata Patch APIs. Reconcile
create/update/mitigate after timeout by querying current state before retrying;
use a deterministic incident identity and existing duplicate protection. Preserve
unrelated metadata fields. A recovery followed by another failure is a new episode
under the same stream; do not accidentally reuse its old failure timestamp.

## Polling and operational behavior

Use paginated GitLab Groups/Projects/Pipelines APIs, bounded request concurrency,
HTTP timeouts, and rate-limit-aware backoff. No overlapping scan for a project.
Recheck current GitLab state before an alert transition; keep failed AlertCenter
operations pending and recompute desired state on retry, so a recovered project
does not receive a stale queued failure notification.

A GitLab timeout, authorization failure, or incomplete pagination is an unknown
observation, never a build failure or recovery. Do not create/escalate/mitigate
from an unhealthy scan. Keep prior state and reconcile when access returns.
Projects removed from scope stop generating mutations; record the reason and
leave any existing incident for explicit handling rather than falsely reporting
recovery. A default-branch change starts a new stream with the same rule.

Write structured stdout logs without tokens: scan coverage, failed requests,
state transitions, and reporting outcomes. Provide a local container health check
based on last completed scan, so a live but stuck process is distinguishable from
a working poller. Do not send bridge infrastructure failures into a recursive
pipeline-alert loop.

## Proposed implementation and acceptance stages

1. Implement a .NET Worker using the existing supported
   company .NET version, HttpClient, and AlertCenter SDK. Do not invoke the old
   count-based `ci report` command: this service uses duration, not failure count.
   Keep the source and multi-stage Dockerfile in this directory. Follow adjacent
   ExternalImages image publication conventions for amd64/arm64.
2. Validate against isolated GitLab responses and an isolated AlertCenter app:
   configured-group/personal isolation; comma parsing, whitespace, duplicates,
   overlapping groups, empty/invalid groups and shared-in project exclusion;
   no incident before 3h; Sev3 at 3h and Sev2 at 8h;
   one failure with no further builds; success/re-failure; out-of-order completion;
   same-pipeline retry; success between scans; cold/warm scans producing identical
   results; startup after 8h creating Sev2 directly; restarts before/after alert;
   pagination, API outages, lost write responses, metadata conflicts and repeated
   reconciliation without duplicate notifications. Use controlled time; no
   intentional company pipeline failures or production notifications.
3. Review and publish the image, then submit the separate Box deployment change
   with two secret references, explicit non-secret `GITLAB_GROUPS=aiursoft`,
   one replica and health check. Deploy only after
   human review and verify real scan coverage before enabling notifications.
   Finish the old CI integration rollback independently.

The approved implementation adds the worker and its ordinary image publication
job. Production deployment and credentials remain a separate reviewed stage.
Validation harnesses stay outside this repository; no test framework is added.

API references: [Pipelines](https://docs.gitlab.com/api/pipelines/) and
[Groups](https://docs.gitlab.com/api/groups/).

## Credential rotation

Follow NugetNinja: read the latest token from disk before using the initial environment token, self-rotate through GitLab, and atomically save the replacement. The long-running bridge rotates once every 24 hours; GitLab supplies its default expiration (normally one week). Keep one writer and stop-before-start deployment. The credential store records its endpoint to prevent reuse against a different server. No token or token prefix is logged.

Persist a rotation intent before sending the non-idempotent request. An interrupted response or persistence failure stops reporting and requires a fresh credential; never retry a possibly revoked token, because GitLab reuse detection can revoke its successors. Disk parse errors do not fall back to the stale environment token. The credential volume does not persist pipeline timing. AlertCenter API keys have no self-rotation protocol in the current SDK and are managed separately.
