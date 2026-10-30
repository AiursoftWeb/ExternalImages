using Aiursoft.AlertCenter.SDK;
using Aiursoft.AlertCenter.SDK.Models;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json.Linq;

namespace TrafficBridge;

internal sealed class Reporter(AlertCenterApiClient api, Settings settings, StateStore store, TimeProvider clock, ILogger<Reporter> log)
{
    internal async Task Scan(Observation? observation, string? failure, long minute, CancellationToken ct)
    {
        if (observation != null && clock.GetUtcNow() - observation.Through > TimeSpan.FromMinutes(3))
        {
            observation = null;
            failure = "StaleObservation";
            store.BreakTrafficSequences();
        }
        var active = new List<IncidentDetails>();
        for (var page = 1; ; page++)
        {
            if (page > 100) throw new InvalidOperationException("Too many source incidents.");
            var response = await api.QueryIncidentsAsync(new QueryIncidentsRequest
            {
                ActiveOnly = true, MetadataKey = "source", MetadataValue = settings.Source, Page = page, PageSize = 100
            }).WaitAsync(ct);
            active.AddRange(response.Items);
            if (active.Count >= response.TotalCount) break;
            if (response.Items.Count == 0) throw new InvalidOperationException("Incomplete incident pagination.");
        }
        if (observation != null && clock.GetUtcNow() - observation.Through > TimeSpan.FromMinutes(3))
            throw new InvalidOperationException("Observation expired during incident query.");
        var current = active.Where(x => x.Status is not ("mitigated" or "resolved"))
            .GroupBy(x => x.Metadata.Value<string>("key") ?? throw new InvalidDataException("Missing incident key."))
            .ToDictionary(x => x.Key, x => x.Single(), StringComparer.Ordinal);
        ResetClosed("collection", current.GetValueOrDefault("collection"));
        var health = store.For("collection");
        var failed = health.Failure.Step(minute, observation == null, 3);
        var healthy = health.Healthy.Step(minute, observation != null, 5);
        await Reconcile("collection", failed ? CollectionReport(failure ?? "Unavailable", minute) : null,
            healthy, null, current.GetValueOrDefault("collection"), minute, ct);
        if (observation == null) return;
        var prefixes = observation.Buckets.Select(x => x.Prefix).Concat(current.Keys.Where(x => x != "collection"))
            .Concat(store.PendingKeys.Where(x => x != "collection")).ToHashSet(StringComparer.Ordinal);
        store.Retain(prefixes.Append("collection").ToHashSet(StringComparer.Ordinal));
        foreach (var prefix in prefixes)
        {
            if (clock.GetUtcNow() - observation.Through > TimeSpan.FromMinutes(3))
                throw new InvalidOperationException("Observation expired during reconciliation.");
            if (!Sources.Included(prefix, settings)) throw new InvalidDataException("Source scope changed while incidents remain pending.");
            ResetClosed(prefix, current.GetValueOrDefault(prefix));
            var sample = observation.For(prefix);
            var trigger = store.For(prefix).Failure.Step(minute, sample.Qualifies, 3);
            var metadata = Metadata(sample, observation.Through);
            var report = trigger ? new CreateIncidentRequest
            {
                ReportId = Guid.NewGuid(), Level = "sev3", Title = $"Sustained concentrated downloads: {prefix}",
                Content = FormattableString.Invariant($"""
                    Server: {settings.Server}
                    Source: {prefix}
                    Window (UTC): {observation.Through.AddMinutes(-10):O} to {observation.Through:O} (end exclusive).
                    Mean response throughput: {sample.BytesPerSecond:F0} B/s; share: {sample.Share:P1}.
                    Top hostnames: {string.Join(", ", sample.TopHosts)}.
                    Three consecutive checks: >=150000 B/s and >=60% share over ten minutes,
                    with at least eight minutes and the latest minute at >=75000 B/s.
                    This reports bandwidth occupancy, not an attack attribution. Legitimate downloads can qualify.
                    Inspect the source and affected applications before taking action. No automatic blocking occurs.
                    Five complete fresh minutes below 75000 B/s automatically mitigate; final resolution is manual.
                    """),
                OccurredAtUtc = observation.Through.AddMinutes(-10), Metadata = Identity(prefix, "downloads", metadata)
            } : null;
            await Reconcile(prefix, report, sample.Recovered, metadata, current.GetValueOrDefault(prefix), minute, ct);
        }
        log.LogInformation("Scanned {Count} public source groups on {Server}", prefixes.Count, settings.Server);
    }

    private async Task Reconcile(string key, CreateIncidentRequest? report, bool recovered, JObject? metadata,
        IncidentDetails? incident, long minute, CancellationToken ct)
    {
        var pending = store.Find(key);
        if (pending != null || incident == null && report != null)
        {
            // Save the original payload and identity before delivery, including when a response is lost.
            pending ??= store.Prepare(key, report!);
            if (pending.ReportId == Guid.Empty || pending.Metadata?.Value<string>("source") != settings.Source || pending.Metadata.Value<string>("key") != key)
                throw new InvalidDataException("Pending report scope mismatch.");
            if (incident != null && incident.ReportId != pending.ReportId)
                throw new InvalidDataException("Conflicting pending report identity.");
            var response = await api.CreateIncidentAsync(pending).WaitAsync(ct);
            var confirmed = (await api.GetIncidentAsync(response.IncidentId).WaitAsync(ct)).Incident;
            if (confirmed.ReportId != pending.ReportId || confirmed.Metadata.Value<string>("key") != key ||
                confirmed.Metadata.Value<string>("source") != settings.Source)
                throw new InvalidDataException("Report identity mismatch.");
            store.Forget(key);
            // A report already closed by a human is never reopened by retrying its durable ID.
            if (confirmed.Status is "mitigated" or "resolved") { store.Reset(key); return; }
            if (incident != null && incident.IncidentId != confirmed.IncidentId)
                throw new InvalidDataException("Multiple open source incidents.");
            incident = confirmed;
        }
        if (incident == null) return;
        if (incident.Metadata.Value<string>("source") != settings.Source || incident.Metadata.Value<string>("key") != key)
            throw new InvalidDataException("Incident identity mismatch.");
        // Re-read before optimistic mutations; human acknowledgement/resolution must win races.
        incident = (await api.GetIncidentAsync(incident.IncidentId).WaitAsync(ct)).Incident;
        if (incident.Status is "mitigated" or "resolved") { store.Reset(key); return; }
        if (incident.Metadata.Value<string>("source") != settings.Source || incident.Metadata.Value<string>("key") != key)
            throw new InvalidDataException("Incident scope changed during reconciliation.");
        store.For(key).Incident = incident.IncidentId;
        if (recovered)
        {
            await api.MitigateIncidentAsync(incident.IncidentId, new MitigateIncidentRequest
            {
                Version = incident.Version,
                Reason = key == "collection" ? "Five consecutive successful, fresh collection checks." :
                    "Five complete, fresh minutes below 75000 B/s for this source. Final resolution remains manual."
            }).WaitAsync(ct);
            store.Reset(key);
            log.LogInformation("Mitigated incident {IncidentId}", incident.IncidentId);
            return;
        }
        var evidence = store.For(key);
        if (metadata != null && minute - evidence.LastUpdate >= 5)
        {
            await api.PatchIncidentMetadataAsync(incident.IncidentId, new PatchIncidentMetadataRequest
            {
                Version = incident.Version, Property = "observation", Value = metadata
            }).WaitAsync(ct);
            evidence.LastUpdate = minute;
        }
    }

    private void ResetClosed(string key, IncidentDetails? incident)
    {
        // Start fresh evidence after a previously observed incident was closed by a human.
        if (store.For(key).Incident is { } id && incident?.IncidentId != id) store.Reset(key);
    }

    private JObject Identity(string key, string kind, JObject? observation) => new()
    {
        ["source"] = settings.Source, ["server"] = settings.Server, ["key"] = key,
        ["kind"] = kind, ["observation"] = observation
    };

    private static JObject Metadata(SourceSample sample, DateTimeOffset through) => new()
    {
        ["throughUtc"] = through, ["bytesPerSecond"] = sample.BytesPerSecond, ["share"] = sample.Share,
        ["staticBytes"] = sample.StaticBytes, ["topHosts"] = new JArray(sample.TopHosts)
    };

    private CreateIncidentRequest CollectionReport(string failure, long minute) => new()
    {
        ReportId = Guid.NewGuid(), Level = "sev3", Title = $"Traffic log collection unavailable: {settings.Server}",
        Content = $"Three consecutive collection failures ({failure}). Download occupancy and recovery are unknown. " +
            "Check ClickHouse connectivity, collection delay and missing log minutes. No download incident is recovered from missing data.",
        OccurredAtUtc = DateTimeOffset.FromUnixTimeSeconds((minute - 2) * 60), Metadata = Identity("collection", "collection", null)
    };
}
