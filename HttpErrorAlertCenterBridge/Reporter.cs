using System.Globalization;
using Aiursoft.AlertCenter.SDK;
using Aiursoft.AlertCenter.SDK.Models;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace HttpErrorBridge;

internal sealed class Reporter(AlertCenterApiClient api, Settings settings, StateStore store, ILogger<Reporter> log, TimeProvider clock)
{
    internal async Task<int> Scan(RecentLog recent, History history, long minute, DateTimeOffset cutoff, CancellationToken ct)
    {
        if (clock.GetUtcNow() - cutoff > TimeSpan.FromMinutes(3))
            throw new InvalidOperationException("Observation is stale.");
        var active = new List<IncidentDetails>();
        for (var page = 1; ; page++)
        {
            var result = await api.QueryIncidentsAsync(new QueryIncidentsRequest
            {
                ActiveOnly = true, MetadataKey = "source", MetadataValue = settings.Source, Page = page, PageSize = 100
            }).WaitAsync(ct);
            active.AddRange(result.Items);
            if (active.Count >= result.TotalCount) break;
            if (result.Items.Count == 0) throw new InvalidOperationException("Incomplete incident pagination.");
        }
        var byHost = active.GroupBy(x => x.Metadata.Value<string>("host") ?? throw new InvalidDataException("Missing incident host."))
            .ToDictionary(x => x.Key, x => x.Single(), StringComparer.Ordinal);
        var buckets = recent.Buckets.ToDictionary(x => (x.Host, x.Start), x => x.Counts);
        var hosts = settings.AllHosts
            ? recent.Buckets.Select(x => x.Host).Concat(byHost.Keys).Concat(store.PendingHosts).ToHashSet(StringComparer.Ordinal)
            : settings.Hosts.ToHashSet(StringComparer.Ordinal);
        store.Retain(hosts);
        foreach (var host in hosts)
        {
            try
            {
                var samples = Enumerable.Range(0, 15).Select(j => buckets.GetValueOrDefault((host, cutoff.ToUnixTimeSeconds() - (15 - j) * 60))).ToArray();
                await Reconcile(host, new Observation(cutoff, samples), history.For(host),
                    byHost.GetValueOrDefault(host), minute, ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (Exception ex)
            {
                store.Reset(host);
                log.LogWarning("Reconciliation failed for {Host} ({ErrorType})", host, ex.GetType().Name);
                throw;
            }
        }
        return hosts.Count;
    }

    internal async Task Reconcile(string host, Observation sample, Baseline? baseline, IncidentDetails? incident, long minute, CancellationToken ct)
    {
        var evidence = store.For(host);
        if (incident == null)
        {
            // Retry the same durable report ID even if the response to an earlier create was lost.
            var trigger = evidence.Failure.Step(minute, sample.Ordinary(baseline), 3) || sample.Blackout;
            if (!trigger) return;
            var reportId = store.Prepare(host);
            var policy = new IncidentPolicy
            {
                Alpha = baseline?.Sev3 ?? 0.01, Beta = baseline?.Sev2, ServerDisplay = settings.Server,
                P95 = baseline?.P95, P99 = baseline?.P99, Days = baseline?.Days ?? 0,
                Samples = baseline?.Samples ?? 0, BaselineAt = baseline?.AsOf
            };
            var response = await api.CreateIncidentAsync(new CreateIncidentRequest
            {
                ReportId = reportId, Level = "sev3", Title = $"HTTP 5xx errors: {host}"[..Math.Min(200, $"HTTP 5xx errors: {host}".Length)],
                Content = Describe(host, sample, policy, sample.Blackout ? "Three complete minutes at or above 90% 5xx, with at least five failures." :
                    "The fifteen-minute error rate exceeded the sev3 threshold for three consecutive checks."),
                OccurredAtUtc = sample.Through.AddMinutes(-15),
                Metadata = new JObject { ["source"] = settings.Source, ["host"] = host, ["server"] = settings.Server, ["policy"] = JObject.FromObject(policy) }
            }).WaitAsync(ct);
            incident = (await api.GetIncidentAsync(response.IncidentId).WaitAsync(ct)).Incident;
            store.Forget(host);
            log.LogInformation("Confirmed sev3 report {ReportId} for {Host}", reportId, host);
        }
        // GetIncident also recovers the case where a timed-out create was already manually closed.
        if (incident.Status is "mitigated" or "resolved")
        {
            store.Reset(host);
            return;
        }
        if (incident.Metadata.Value<string>("source") != settings.Source || incident.Metadata.Value<string>("host") != host)
            throw new InvalidDataException("Incident identity mismatch.");
        if (incident.ReceivedAtUtc == default || incident.ReceivedAtUtc > clock.GetUtcNow().AddMinutes(1))
            throw new InvalidDataException("Invalid incident creation time.");
        if (evidence.Incident != incident.IncidentId)
        {
            store.Reset(host);
            evidence = store.For(host);
            evidence.Incident = incident.IncidentId;
        }
        var frozen = incident.Metadata["policy"]?.ToObject<IncidentPolicy>() ?? throw new InvalidDataException("Missing incident policy.");
        frozen.Validate();
        if (frozen.Beta == null && baseline != null)
        {
            // A cold-start blackout may learn its escalation threshold once, without changing recovery.
            frozen.Beta = baseline.Sev2;
            frozen.P99 = baseline.P99;
            frozen.Days = baseline.Days;
            frozen.Samples = baseline.Samples;
            frozen.BaselineAt = baseline.AsOf;
            await api.PatchIncidentMetadataAsync(incident.IncidentId, new PatchIncidentMetadataRequest
            {
                Version = incident.Version, Property = "policy", Value = JObject.FromObject(frozen)
            }).WaitAsync(ct);
            incident = (await api.GetIncidentAsync(incident.IncidentId).WaitAsync(ct)).Incident;
            if (incident.Status is "mitigated" or "resolved") { store.Reset(host); return; }
        }
        if (evidence.Healthy.Step(minute, sample.Healthy(frozen.Alpha), 5))
        {
            await api.MitigateIncidentAsync(incident.IncidentId, new MitigateIncidentRequest
            {
                Version = incident.Version,
                Reason = Describe(host, sample, frozen, "Five consecutive healthy checks; fifteen-minute and five-minute rates are below the recovery thresholds.")
            }).WaitAsync(ct);
            store.Reset(host);
            log.LogInformation("Mitigated incident {IncidentId} for {Host}", incident.IncidentId, host);
            return;
        }
        var severe = evidence.Severe.Step(minute, sample.Severe(frozen.Beta), 3);
        // ReceivedAtUtc is authoritative. Never use OccurredAtUtc or a locally reconstructed onset.
        if (incident.Level == "sev3" && severe && clock.GetUtcNow() - incident.ReceivedAtUtc >= TimeSpan.FromMinutes(35))
        {
            await api.ChangeIncidentSeverityAsync(incident.IncidentId, new ChangeIncidentSeverityRequest
            {
                Version = incident.Version, Level = "sev2",
                Reason = Describe(host, sample, frozen, "The sev3 incident is at least 35 minutes old and the sev2 conditions remain satisfied.")
            }).WaitAsync(ct);
            log.LogInformation("Escalated incident {IncidentId} for {Host}", incident.IncidentId, host);
        }
    }

    internal static string Describe(string host, Observation sample, IncidentPolicy policy, string action)
    {
        static string Percent(double? value) => value.HasValue ? value.Value.ToString("P3", CultureInfo.InvariantCulture) : "unavailable";
        return FormattableString.Invariant($"""
            Application: {host}
            Server: {policy.ServerDisplay}
            Window (UTC): {sample.Through.AddMinutes(-15):yyyy-MM-dd HH:mm:ss} to {sample.Through:yyyy-MM-dd HH:mm:ss} (end exclusive)
            HTTP 5xx: {sample.Window.Errors} of {sample.Window.Requests} requests; error rate: {Percent(sample.Window.Rate)}.
            Historical P95: {Percent(policy.P95)}; P99: {Percent(policy.P99)}.
            Baseline: {policy.Days} observed UTC days, {policy.Samples} windows; refreshed at {policy.BaselineAt:O}.
            Incident thresholds: sev3 {Percent(policy.Alpha)}; sev2 {Percent(policy.Beta)}; recovery below {Percent(policy.Alpha / 2)}.
            {action}

            Next steps: inspect application exceptions, reverse-proxy upstream connectivity and recent deployments for this hostname.
            Automated recovery mitigates this incident; final resolution remains a human decision.
            """);
    }
}

internal sealed class IncidentPolicy
{
    public double Alpha { get; set; }
    public double? Beta { get; set; }
    public double? P95 { get; set; }
    public double? P99 { get; set; }
    public int Days { get; set; }
    public int Samples { get; set; }
    public DateTimeOffset? BaselineAt { get; set; }
    public string ServerDisplay { get; set; } = "";

    internal void Validate()
    {
        if (!double.IsFinite(Alpha) || Alpha is < 0.01 or > 1 ||
            (Beta.HasValue && (!double.IsFinite(Beta.Value) || Beta.Value is < 0.05 or > 1)))
            throw new InvalidDataException("Invalid incident thresholds.");
    }
}
