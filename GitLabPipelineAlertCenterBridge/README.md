# GitLab Pipeline AlertCenter Bridge

Scans selected GitLab groups' default-branch pipelines and reports prolonged failures to AlertCenter: Sev3 after 3 hours, Sev2 after 8 hours, automatic mitigation on recovery. GitLab history supplies failure duration; no database or repository CI changes are needed.

Run one replica with `GITLAB_GROUPS=aiursoft`, `GITLAB_ACCESS_TOKEN` and `ALERTCENTER_API_KEY`. Groups are comma-separated paths, including subgroups. Optional `GITLAB_URL` and `ALERTCENTER_URL` override the Aiursoft endpoints.
