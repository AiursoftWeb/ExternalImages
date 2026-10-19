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
        foreach (var incident in incidents)
        {
            var iid = (incident.Metadata["ninja"] as JObject)?.Value<long>("iid");
            var source = incident.Metadata.Value<string>("source");
            if (source != projectSource && source != $"{projectSource}:{iid}")
                throw new InvalidOperationException("Invalid NugetNinja source.");
        }
        var merged = await gitlab.LatestNinjaMerge(project, ct);
        var boundary = merged?.MergedAt ?? DateTimeOffset.MinValue;
        var pending = (await gitlab.NinjaMergeRequests(project, ct))
            .Where(x => x.CreatedAt > boundary).OrderBy(x => x.CreatedAt).ThenBy(x => x.Iid).ToList();
        var active = incidents.Where(Active).ToList();
        // A successful merge recovers the repository, including incidents for deleted/replaced MRs.
        foreach (var incident in active.Where(x => Since(x) < boundary).ToList())
        {
            if (await gitlab.LatestNinjaMerge(project, ct) != merged)
                throw new InvalidOperationException("NugetNinja merge history changed during reconciliation.");
            await api.MitigateIncidentAsync(incident.IncidentId, new MitigateIncidentRequest
            {
                Version = incident.Version,
                Reason = $"Repository compliance automation recovered: NugetNinja MR !{merged!.Iid} merged at {boundary:O}: {merged.WebUrl}"
            }).WaitAsync(ct);
            active.Remove(incident);
        }
        // Human mitigation/resolution suppresses the repository episode until a later successful merge.
        if (active.Count == 0 && incidents.Any(x => !Active(x) && Since(x) > boundary)) return;
        var starts = active.Select(Since).Concat(pending.Select(x => x.CreatedAt)).ToList();
        if (starts.Count == 0) return;
        var since = starts.Min();
        var age = DateTimeOffset.UtcNow - since;
        if (since == default || age < TimeSpan.Zero) throw new InvalidOperationException("Invalid NugetNinja failure time.");
        if (age < TimeSpan.FromHours(24)) return;
        var mr = pending.Count == 0 ? null : await gitlab.ReadMergeRequest(project, pending[0].Iid, ct);
        var snapshot = active.FirstOrDefault()?.Metadata["ninja"]?.DeepClone() as JObject ?? new JObject();
        var files = new List<string>();
        var jobs = new List<PipelineJob>();
        var currentPipeline = false;
        if (mr != null)
        {
            if (mr.Iid != pending[0].Iid || mr.TargetProjectId != project.Id || mr.Author.Username != "nuget-ninja" ||
                mr.State != "opened" || mr.CreatedAt == default || mr.CreatedAt > DateTimeOffset.UtcNow || mr.CreatedAt != pending[0].CreatedAt || string.IsNullOrWhiteSpace(mr.Sha) || string.IsNullOrWhiteSpace(mr.WebUrl))
                throw new InvalidOperationException("NugetNinja merge request changed during reconciliation.");
            files = await gitlab.ChangedFiles(project, mr.Iid, ct);
            currentPipeline = mr.HeadPipeline != null && mr.HeadPipeline.Sha == mr.Sha;
            if (currentPipeline && mr.HeadPipeline!.Status == "failed") jobs = await gitlab.FailedJobs(project, mr.HeadPipeline.Id, ct);
            if (await gitlab.ReadMergeRequest(project, mr.Iid, ct) != mr)
                throw new InvalidOperationException("Merge request changed during reconciliation.");
            snapshot = Snapshot(mr, currentPipeline, jobs);
            snapshot["changedFiles"] = new JArray(files);
        }
        else snapshot["summary"] = "No open NugetNinja MR is currently visible. Closing or deleting a request does not confirm recovery; a subsequent successful NugetNinja merge is still required.";
        snapshot["failedSince"] = since.ToString("O");
        if (await gitlab.LatestNinjaMerge(project, ct) != merged)
            throw new InvalidOperationException("NugetNinja merge history changed during reconciliation.");
        if (active.Count == 0)
        {
            // Always create Sev3 first. An already-old episode is escalated on the next scan, never recreated.
            var title = $"NugetNinja compliance stalled: {project.Name}";
            var content = $"NugetNinja automates project compliance. Timely merging of its changes is essential to maintaining compliance.\n\n" +
                $"Repository compliance stalled since {since:O} ({Math.Floor(age.TotalHours)} hours at observation {DateTimeOffset.UtcNow:O}).\n" +
                $"Proposed change: {Limit(mr!.Title, 300)}\nMR description: {Limit(mr.Description ?? "Not provided.", 600)}\n" +
                $"Changed files (up to 10): {Limit(string.Join(", ", files), 600)}\n" +
                $"Observed merge status: {snapshot.Value<string>("summary")}\nMR: {mr.WebUrl}\n" +
                $"Pipeline: {(currentPipeline ? mr.HeadPipeline!.WebUrl : "No pipeline for the current MR head.")}\n" +
                string.Join("\n", jobs.Take(5).Select(x => $"Failed required job: {Limit(x.Name, 100)} ({x.FailureReason ?? "reason unavailable"}) — {x.WebUrl}")) +
                "\n\nOCE: investigate the blocker and drive a NugetNinja compliance MR to merge in this repository. " +
                "A subsequent successful NugetNinja merge recovers this incident, including a replacement MR. Closing/deleting alone does not. " +
                "See ninja metadata for the latest observed snapshot.";
            await api.CreateIncidentAsync(new CreateIncidentRequest
            {
                ReportId = new Guid(SHA256.HashData(Encoding.UTF8.GetBytes(projectSource + ":" + since.ToString("O"))).AsSpan(0, 16)),
                Title = Limit(title, 200), Content = Limit(content, 4000), Level = "sev3", OccurredAtUtc = since,
                Metadata = new JObject { ["source"] = projectSource, ["ninjaProject"] = projectSource, ["ninja"] = snapshot }
            }).WaitAsync(ct);
            log.LogInformation("Created Sev3 NugetNinja incident for project {ProjectId}", project.Id);
            return;
        }
        foreach (var incident in active)
        {
            var current = incident;
            if (age >= TimeSpan.FromDays(7) && current.Level is "sev3" or "sev4")
            {
                await api.ChangeIncidentSeverityAsync(current.IncidentId, new ChangeIncidentSeverityRequest
                {
                    Version = current.Version, Level = "sev2",
                    Reason = $"Repository NugetNinja compliance has remained stalled since {since:O}, at least 7 days. {snapshot.Value<string>("summary")}"
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

    private static DateTimeOffset Since(IncidentDetails incident)
    {
        var state = incident.Metadata["ninja"] as JObject ?? throw new InvalidOperationException("Missing NugetNinja metadata.");
        return DateTimeOffset.Parse(state.Value<string>("failedSince") ?? state.Value<string>("createdAt")
            ?? throw new InvalidOperationException("Missing NugetNinja failure time."), System.Globalization.CultureInfo.InvariantCulture);
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
