# GitLab Pipeline AlertCenter Bridge — proposed design

Status: design for review; no implementation or deployment in this change.

Run one continuously running container, `gitlab_pipeline_alert_center_bridge`,
from ExternalImages. Observe company default-branch pipelines through GitLab's
read-only API and report prolonged failures to AlertCenter. Monitored repositories
need no CI changes, includes, installed tools, webhooks, or credentials.

## Scope and configuration

- Discover active projects owned by the `aiursoft` group and its subgroups.
  Exclude shared-in projects, personal namespaces (including `anduin`), bot forks,
  archived projects, tags, MR pipelines, child pipelines, and non-default branches.
  Verify ownership independently of what the token can access.
- Monitor the overall top-level pipeline result, including build, test and deploy
  failures. Allowed failures whose overall pipeline succeeds do not start alerts.
- Required secrets: `GITLAB_ACCESS_TOKEN` (read API) and
  `ALERTCENTER_API_KEY` (one dedicated application for the entire bridge).
- Default endpoints: `https://gitlab.aiursoft.com` and
  `https://alertcenter.aiursoft.com`; allow non-secret endpoint overrides for
  isolated acceptance. Do not reuse the revoked CI reporting key.
- No UI, incoming webhook, database, volume, or per-repository configuration.
  Proposed initial intervals: pipeline scan every 60 seconds, discovery every
  10 minutes; thresholds 3 and 8 hours. Keep these as simple service defaults.

## Failure-duration policy

Each project/default-branch pair has an independent failure episode.

| Observation | Action |
| --- | --- |
| First observed failure | Start an in-memory UTC timer; create nothing |
| Still failing for less than 3 hours | Keep observing |
| Still failing for at least 3 hours | Create one Sev3 incident |
| Still failing for at least 8 hours | Escalate that incident to Sev2 |
| Newer successful pipeline | Reset timer; mitigate the existing incident, or skip if none |
| Running, pending, manual, canceled or skipped pipeline | Neither start a failure nor declare recovery |

A single failed pipeline left unfixed crosses the thresholds without another
build. Further failures do not restart the clock. New builds in progress do not
reset an existing failure episode. Recovery means a successful pipeline, not
merely a new commit or cancellation. Threshold actions occur on the next healthy
scan, not at an exact wall-clock deadline. Preserve manually assigned Sev0/Sev1.

Use pipeline ID ordering to prevent an older pipeline completing late from
reopening a failure after a newer success. Re-read tracked pipeline IDs so retrying
the same failed pipeline to success also recovers. Read all newly relevant pages,
not just the newest result, so success followed by failure between scans starts a
new episode. Process observations serially per project. Record both pipeline IDs
and observed statuses; an ID alone cannot deduplicate same-pipeline retries.

The first release measures *observed* failure duration. It does not reconstruct
historical outages at first startup. A project already failing starts its timer
when first observed. Polling cannot recover transient states overwritten entirely
between scans; document this rather than claiming an exact event history.

## Memory and restart behavior

Memory holds discovery cache, pipeline watermarks/statuses, failure start time,
current incident reference, and pending reconciliation. There is no local durable
storage. Run exactly one replica with stop-before-start replacement.

Restart loses unreported timers: a failure not yet reported must remain observed
for another 3 hours. Repeated restarts can therefore postpone an alert. This is an
explicit proposed tradeoff of memory-only state, requiring design acceptance.

Reported incidents remain in AlertCenter. On startup, find bridge-owned active
incidents by exact stable identity (bridge source, GitLab instance, project ID,
branch); reuse their recorded failure start time and reconcile current GitLab
state. A healthy project mitigates its incident; a still-failing project retains
its age. Never adopt legacy CI incidents or unrelated application incidents.

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

1. After design approval, implement a .NET Worker using the existing supported
   company .NET version, HttpClient, and AlertCenter SDK. Do not invoke the old
   count-based `ci report` command: this service uses duration, not failure count.
   Keep the source and multi-stage Dockerfile in this directory. Follow adjacent
   ExternalImages image publication conventions for amd64/arm64.
2. Validate against isolated GitLab responses and an isolated AlertCenter app:
   company/personal isolation; no incident before 3h; Sev3 at 3h and Sev2 at 8h;
   one failure with no further builds; success/re-failure; out-of-order completion;
   same-pipeline retry; success between scans; restarts before/after alert;
   pagination, API outages, lost write responses, metadata conflicts and repeated
   reconciliation without duplicate notifications. Use controlled time; no
   intentional company pipeline failures or production notifications.
3. Review and publish the image, then submit the separate Box deployment change
   with two secret references, one replica and health check. Deploy only after
   human review and verify real scan coverage before enabling notifications.
   Finish the old CI integration rollback independently.

This plan does not authorize adding runtime code, deployment, new credentials,
CI checks, or a test framework. Decide the restart tradeoff before implementation.

API references: [Pipelines](https://docs.gitlab.com/api/pipelines/) and
[Groups](https://docs.gitlab.com/api/groups/).
