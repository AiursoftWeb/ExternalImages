using System.Security.Cryptography;
using System.Text;
using Aiursoft.AlertCenter.SDK;
using Aiursoft.AlertCenter.SDK.Models;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace PipelineBridge;

internal sealed class NinjaReporter(AlertCenterApiClient api, GitLab gitlab, Settings settings, ILogger<NinjaReporter> log)
{
    internal async Task Reconcile(Project project, CancellationToken ct)
    {
        var projectSource = $"gitlab-nugetninja-bridge:v1:{settings.GitLabUrl}:{project.Id}";
        var incidents = new List<IncidentDetails>();
        for (var page = 1; ; page++)
        {
            var result = await api.QueryIncidentsAsync(new QueryIncidentsRequest
            {
                MetadataKey = "ninjaProject", MetadataValue = projectSource, Page = page, PageSize = 100
            }).WaitAsync(ct);
            incidents.AddRange(result.Items);
            if (incidents.Count >= result.TotalCount) break;
            if (result.Items.Count == 0) throw new InvalidOperationException("Incomplete AlertCenter pagination.");
        }
        var candidates = (await gitlab.NinjaMergeRequests(project, ct)).ToHashSet();
        // Re-read tracked requests even after closing/merging and after a process restart.
        foreach (var incident in incidents.Where(Active))
        {
            var iid = (incident.Metadata["ninja"] as JObject)?.Value<long>("iid")
                ?? throw new InvalidOperationException("Missing NugetNinja metadata.");
            if (iid <= 0 || incident.Metadata.Value<string>("source") != $"{projectSource}:{iid}")
                throw new InvalidOperationException("Invalid NugetNinja source.");
            candidates.Add(iid);
        }
        foreach (var iid in candidates.Order())
        {
            var source = $"{projectSource}:{iid}";
            var existing = incidents.Where(x => x.Metadata.Value<string>("source") == source).ToList();
            var mr = await gitlab.ReadMergeRequest(project, iid, ct);
            if (mr.Iid != iid || mr.TargetProjectId != project.Id || mr.Author.Username != "nuget-ninja" ||
                mr.CreatedAt == default || mr.CreatedAt > DateTimeOffset.UtcNow || string.IsNullOrWhiteSpace(mr.WebUrl) || string.IsNullOrWhiteSpace(mr.Sha))
                throw new InvalidOperationException("Invalid NugetNinja merge request.");
            if (mr.State == "merged")
            {
                if (mr.MergedAt == null) throw new InvalidOperationException("Merged request has no merge time.");
                foreach (var incident in existing.Where(Active))
                    await api.MitigateIncidentAsync(incident.IncidentId, new MitigateIncidentRequest
                    {
                        Version = incident.Version, Reason = $"NugetNinja MR confirmed merged at {mr.MergedAt:O}: {mr.WebUrl}"
                    }).WaitAsync(ct);
                continue;
            }
            if (mr.State is not ("opened" or "closed" or "locked"))
                throw new InvalidOperationException("Unknown merge request state.");
            var age = DateTimeOffset.UtcNow - mr.CreatedAt;
            if (age < TimeSpan.FromHours(24)) continue;
            // Respect a human mitigation/resolution. Never recreate this MR's incident.
            if (existing.Count > 0 && !existing.Any(Active)) continue;
            if (existing.Count == 0 && mr.State != "opened") continue;
            var files = await gitlab.ChangedFiles(project, iid, ct);
            var pipeline = mr.HeadPipeline;
            var currentPipeline = pipeline != null && pipeline.Sha == mr.Sha;
            var jobs = currentPipeline && pipeline!.Status == "failed"
                ? await gitlab.FailedJobs(project, pipeline.Id, ct) : [];
            // The job lookup may race a refresh or merge. Never report its old head as current.
            var latest = await gitlab.ReadMergeRequest(project, iid, ct);
            if (latest != mr) throw new InvalidOperationException("Merge request changed during reconciliation.");
            var snapshot = Snapshot(mr, currentPipeline, jobs);
            snapshot["changedFiles"] = new JArray(files);
            var level = age >= TimeSpan.FromDays(7) ? "sev2" : "sev3";
            if (existing.Count == 0)
            {
                var title = $"NugetNinja compliance MR unmerged: {project.Name} !{iid}";
                var content = $"NugetNinja automates project compliance. Timely merging of its changes is essential to maintaining compliance.\n\n" +
                    $"Proposed change: {Limit(mr.Title, 300)}\nMR description: {Limit(mr.Description ?? "Not provided.", 600)}\nCreated: {mr.CreatedAt:O}\n" +
                    $"Unmerged for {Math.Floor(age.TotalHours)} hours at observation ({DateTimeOffset.UtcNow:O}).\n" +
                    $"Changed files (up to 10): {Limit(string.Join(", ", files), 600)}\n" +
                    $"Observed merge status: {snapshot.Value<string>("summary")}\nMR: {mr.WebUrl}\n" +
                    $"Pipeline: {(currentPipeline ? pipeline!.WebUrl : "No pipeline for the current MR head.")}\n" +
                    string.Join("\n", jobs.Take(5).Select(x => $"Failed required job: {Limit(x.Name, 100)} ({x.FailureReason ?? "reason unavailable"}) — {x.WebUrl}")) +
                    "\n\nOCE: investigate the blocker, repair the compliance change or its checks, and drive the MR to merge. " +
                    "See the MR for live status and ninja metadata for the latest observed snapshot. Closing without merging does not confirm compliance recovery.";
                await api.CreateIncidentAsync(new CreateIncidentRequest
                {
                    ReportId = new Guid(SHA256.HashData(Encoding.UTF8.GetBytes(source)).AsSpan(0, 16)),
                    Title = Limit(title, 200), Content = Limit(content, 4000), Level = level, OccurredAtUtc = mr.CreatedAt,
                    Metadata = new JObject { ["source"] = source, ["ninjaProject"] = projectSource, ["ninja"] = snapshot }
                }).WaitAsync(ct);
                log.LogInformation("Created {Level} NugetNinja incident for project {ProjectId} MR {Iid}", level, project.Id, iid);
                continue;
            }
            foreach (var incident in existing.Where(Active))
            {
                var current = incident;
                if (level == "sev2" && current.Level is "sev3" or "sev4")
                {
                    await api.ChangeIncidentSeverityAsync(current.IncidentId, new ChangeIncidentSeverityRequest
                    {
                        Version = current.Version, Level = level,
                        Reason = $"NugetNinja MR has remained unmerged for at least 7 days. {snapshot.Value<string>("summary")} MR: {mr.WebUrl}"
                    }).WaitAsync(ct);
                    current = (await api.GetIncidentAsync(current.IncidentId).WaitAsync(ct)).Incident;
                }
                if (Active(current) && !JToken.DeepEquals(current.Metadata["ninja"], snapshot))
                    await api.PatchIncidentMetadataAsync(current.IncidentId, new PatchIncidentMetadataRequest
                    {
                        Version = current.Version, Property = "ninja", Value = snapshot
                    }).WaitAsync(ct);
            }
        }
    }

    private static JObject Snapshot(MergeRequest mr, bool currentPipeline, List<PipelineJob> jobs)
    {
        var status = mr.DetailedMergeStatus ?? "unknown";
        var summary = $"MR state={mr.State}; merge check={status}; conflicts={mr.HasConflicts}; draft={mr.Draft}. ";
        summary += !currentPipeline ? "No pipeline for the current MR head; CI failure is not established."
            : mr.HeadPipeline!.Status == "failed" ? "The current MR head pipeline failed; inspect the linked jobs."
            : $"Current MR head pipeline status={mr.HeadPipeline!.Status}; investigate the remaining merge requirements.";
        if (mr.State == "closed") summary += " Closed without merging; compliance recovery is unconfirmed.";
        return new JObject
        {
            ["iid"] = mr.Iid, ["createdAt"] = mr.CreatedAt.ToString("O"), ["title"] = mr.Title,
            ["mrUrl"] = mr.WebUrl, ["sha"] = mr.Sha, ["state"] = mr.State, ["summary"] = summary,
            ["pipelineId"] = currentPipeline ? mr.HeadPipeline!.Id : null,
            ["pipelineUrl"] = currentPipeline ? mr.HeadPipeline!.WebUrl : null,
            ["pipelineStatus"] = currentPipeline ? mr.HeadPipeline!.Status : null,
            ["failedJobs"] = new JArray(jobs.Select(x => new JObject { ["name"] = x.Name, ["reason"] = x.FailureReason, ["url"] = x.WebUrl }))
        };
    }

    private static bool Active(IncidentDetails incident) => incident.Status is not ("mitigated" or "resolved");
    private static string Limit(string value, int length) => value.Length > length ? value[..length] : value;
}
