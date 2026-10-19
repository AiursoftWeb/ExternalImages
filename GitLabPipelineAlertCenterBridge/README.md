# GitLab Pipeline AlertCenter Bridge

Scans selected GitLab groups’ default-branch pipelines: Sev3 after 3 hours of failure, Sev2 after 8 hours, and automatic mitigation on recovery. Also monitors NugetNinja merge requests: Sev3 after 24 hours unmerged, Sev2 after 7 days, and mitigation only after confirmed merge. Closing without merging keeps an existing alert active. No repository CI changes are needed.

Run one replica with `GITLAB_URL`, `ALERTCENTER_URL`, `GITLAB_GROUPS` (comma-separated group paths), `GITLAB_ACCESS_TOKEN` and `ALERTCENTER_API_KEY`. Use a dedicated GitLab personal access token with `read_api` and `self_rotate`; it rotates daily. AlertCenter keys are managed separately.

Mount writable persistent storage at `/data` for UID 1654 (override with `TOKEN_DIRECTORY`). Saved tokens take precedence over the initial environment token. After an interrupted rotation, stop the service, supply a fresh token and remove `gitlab-token.json` before restarting. Pipeline history remains authoritative; the volume stores credentials only.
