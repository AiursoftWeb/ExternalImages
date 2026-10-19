using System.Security.Cryptography;
using System.Text;
using Aiursoft.AlertCenter.SDK;
using Aiursoft.AlertCenter.SDK.Models;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace PipelineBridge;

internal sealed class Reporter(AlertCenterApiClient api, GitLab gitlab, Settings settings, ILogger<Reporter> log)
{
    internal async Task Reconcile(Project project, BranchState expected, CancellationToken ct)
    {
        var source = $"gitlab-pipeline-bridge:v1:{settings.GitLabUrl}:{project.Id}:{project.DefaultBranch}";
        var incidents = new List<IncidentDetails>();
        for (var page = 1; ; page++)
        {
            var result = await api.QueryIncidentsAsync(new QueryIncidentsRequest
            {
                MetadataKey = "source", MetadataValue = source, Page = page, PageSize = 100
            }).WaitAsync(ct);
            incidents.AddRange(result.Items);
            if (incidents.Count >= result.TotalCount) break;
            if (result.Items.Count == 0) throw new InvalidOperationException("Incomplete AlertCenter pagination.");
        }
        var current = await gitlab.Read(project, ct);
        if (current == null || current.Fingerprint != expected.Fingerprint)
            throw new InvalidOperationException("GitLab state changed during reconciliation.");
        var failed = current.Latest.Status == "failed";
        var episode = failed ? $"{current.SuccessBoundary}:{current.FirstFailureId}:{current.FailedSince:O}" : null;
        var reportId = Identity(source + ":" + episode);
        var reason = $"GitLab default branch pipeline {current.Latest.Id}: {current.Latest.WebUrl}";

        foreach (var incident in incidents.Where(Active))
        {
            var previous = incident.Metadata["bridge"] as JObject ?? throw new InvalidOperationException("Missing bridge metadata.");
            var previousPipeline = previous.Value<long>("pipelineId");
            // A still-running retry must not be recovered by an older successful pipeline.
            if (current.Latest.Id < previousPipeline) continue;
            var previousEpisode = previous.Value<string>("episode");
            if (!failed || (previousEpisode != episode && current.SuccessBoundary >= previousPipeline))
            {
                await api.MitigateIncidentAsync(incident.IncidentId, new MitigateIncidentRequest
                {
                    Version = incident.Version, Reason = reason
                }).WaitAsync(ct);
                log.LogInformation("Mitigated incident {IncidentId} for project {ProjectId}", incident.IncidentId, project.Id);
            }
        }
        if (!failed) return;
        var age = DateTimeOffset.UtcNow - current.FailedSince!.Value;
        if (age < TimeSpan.FromHours(3)) return;
        var level = age >= TimeSpan.FromHours(8) ? "sev2" : "sev3";
        var metadata = new JObject
        {
            ["episode"] = episode, ["failedSince"] = current.FailedSince.Value.ToString("O"),
            ["pipelineId"] = current.Latest.Id, ["pipelineUrl"] = current.Latest.WebUrl
        };
        // Reuse an active incident even if mutable GitLab retry timestamps changed the computed identity.
        var existing = incidents.FirstOrDefault(x => x.ReportId == reportId) ?? incidents.FirstOrDefault(x =>
            Active(x) && ((JObject)x.Metadata["bridge"]!).Value<long>("pipelineId") > current.SuccessBoundary);
        if (existing == null)
        {
            var title = $"GitLab pipeline failing: {project.Name} ({project.DefaultBranch})";
            await api.CreateIncidentAsync(new CreateIncidentRequest
            {
                ReportId = reportId, Title = title.Length > 200 ? title[..200] : title,
                Content = reason, Level = level, OccurredAtUtc = current.FailedSince.Value,
                Metadata = new JObject { ["source"] = source, ["bridge"] = metadata }
            }).WaitAsync(ct);
            log.LogInformation("Created {Level} incident for project {ProjectId}", level, project.Id);
            return;
        }
        // Manual mitigation/resolution of this exact episode is respected.
        if (!Active(existing)) return;
        if (existing.Metadata["bridge"] is JObject previousState && previousState.Value<long>("pipelineId") > current.Latest.Id) return;
        if (level == "sev2" && existing.Level is "sev3" or "sev4")
        {
            await api.ChangeIncidentSeverityAsync(existing.IncidentId, new ChangeIncidentSeverityRequest
            {
                Version = existing.Version, Level = level, Reason = reason
            }).WaitAsync(ct);
            // Re-query before a metadata patch; severity changed the optimistic concurrency version.
            existing = (await api.GetIncidentAsync(existing.IncidentId).WaitAsync(ct)).Incident;
            log.LogInformation("Escalated incident {IncidentId} to Sev2", existing.IncidentId);
        }
        if (!JToken.DeepEquals(existing.Metadata["bridge"], metadata))
            await api.PatchIncidentMetadataAsync(existing.IncidentId, new PatchIncidentMetadataRequest
            {
                Version = existing.Version, Property = "bridge", Value = metadata
            }).WaitAsync(ct);
    }

    private static bool Active(IncidentDetails incident) => incident.Status is not ("mitigated" or "resolved");
    private static Guid Identity(string input) => new(SHA256.HashData(Encoding.UTF8.GetBytes(input)).AsSpan(0, 16));
}
