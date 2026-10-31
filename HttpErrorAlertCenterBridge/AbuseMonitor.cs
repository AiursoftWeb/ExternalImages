using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Aiursoft.AlertCenter.SDK;
using Aiursoft.AlertCenter.SDK.Models;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace HttpErrorBridge;

internal sealed class AbuseStore(Settings settings)
{
    private AbuseState? _state;
    private readonly Dictionary<string, (Sequence Slow, Sequence Long, Sequence Fast)> _sequences = new(StringComparer.Ordinal);
    private string PathName => Path.Combine(settings.DataDirectory, "abuse.json");
    internal string Scope
    {
        get
        {
            if (!Guid.TryParse(settings.AlertCenterKey.Split('.')[0], out var application))
                throw new InvalidDataException("An application-scoped key is required for abuse monitoring.");
            return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join('\n', settings.AbuseSource,
                settings.AlertCenterUrl, application.ToString(), string.Join(',', settings.Hosts),
                string.Join(',', settings.AbuseExcludedSources.Select(x => x.ToString()).Order(StringComparer.Ordinal))))));
        }
    }
    internal AbuseState State
    {
        get
        {
            if (_state != null) return _state;
            var state = File.Exists(PathName) ? System.Text.Json.JsonSerializer.Deserialize<AbuseState>(File.ReadAllBytes(PathName))
                ?? throw new InvalidDataException("Invalid abuse state.") : new AbuseState { Scope = Scope };
            if (state.Scope != Scope || state.Subjects.Concat(state.Suppressed).Any(x => !AbuseSources.Included(x, settings)))
                throw new InvalidDataException("Abuse state scope changed; retain and reconcile existing incidents first.");
            return _state = state;
        }
    }
    internal void Save()
    {
        Directory.CreateDirectory(settings.DataDirectory);
        StateStore.AtomicWrite(PathName, State);
    }
    internal void BreakSequences() => _sequences.Clear();
    internal void Retain(IEnumerable<string> sources)
    {
        var keep = sources.Concat(State.Subjects).Concat(State.Suppressed).ToHashSet(StringComparer.Ordinal);
        foreach (var key in _sequences.Keys.Where(x => !keep.Contains(x)).ToArray()) _sequences.Remove(key);
    }
    internal bool Step(string source, long minute, bool slow, bool longer, bool fast)
    {
        if (!_sequences.TryGetValue(source, out var sequences)) _sequences[source] = sequences = (new(), new(), new());
        // Evaluate every sequence even when an earlier branch already matches.
        var a = sequences.Slow.Step(minute, slow, 3);
        var b = sequences.Long.Step(minute, longer, 3);
        var c = sequences.Fast.Step(minute, fast, 2);
        return a || b || c;
    }
    internal CreateIncidentRequest Report(AbuseDelivery delivery)
    {
        var request = JsonConvert.DeserializeObject<CreateIncidentRequest>(delivery.Payload)
            ?? throw new InvalidDataException("Invalid queued abuse report.");
        if (request.ReportId == Guid.Empty || string.IsNullOrWhiteSpace(request.Content) || request.Metadata == null || request.Metadata.Value<string>("scope") != Scope ||
            request.Metadata.Value<string>("source") != settings.AbuseSource || request.Metadata.Value<string>("server") != settings.Server ||
            delivery.Subjects.Count == 0 || delivery.Subjects.Any(x => !AbuseSources.Included(x, settings)))
            throw new InvalidDataException("Queued abuse report scope mismatch.");
        return request;
    }

}

internal sealed class AbuseState
{
    public string Scope { get; set; } = "";
    public Guid? Incident { get; set; }
    public HashSet<string> Subjects { get; set; } = new(StringComparer.Ordinal);
    public HashSet<string> Suppressed { get; set; } = new(StringComparer.Ordinal);
    public List<AbuseDelivery> Outbox { get; set; } = [];
    public string Level { get; set; } = "";
    public long LastMetadata { get; set; }
}

internal sealed class AbuseDelivery
{
    public string Payload { get; set; } = "";
    public bool Started { get; set; }
    public HashSet<string> Subjects { get; set; } = new(StringComparer.Ordinal);
}

internal sealed record AbuseFinding(AbuseSample Sample, string Reason, AbusePaths? Paths);

internal sealed class AbuseReporter(AlertCenterApiClient api, Settings settings, AbuseStore store, RequestLog db,
    TimeProvider clock, ILogger<AbuseReporter> log)
{
    internal async Task Scan(AbuseObservation observation, long minute, CancellationToken ct)
    {
        observation.Validate(clock.GetUtcNow());
        var state = store.State;
        foreach (var prefix in state.Suppressed.Where(x => observation.For(x).Recovered).ToArray()) state.Suppressed.Remove(prefix);
        store.Retain(observation.Samples.Keys);
        var slow = observation.Samples.Values.Where(x => x.Slow(20) && !state.Suppressed.Contains(x.Prefix)).Select(x => x.Prefix).ToArray();
        var longer = observation.Samples.Values.Where(x => x.Slow(60) && !state.Suppressed.Contains(x.Prefix)).Select(x => x.Prefix).ToArray();
        // Path failures invalidate only the dependent branches, never the independent fast detector.
        var (paths20, ok20) = await Paths(20, slow);
        var (paths60, ok60) = await Paths(60, longer);
        var findings = new List<AbuseFinding>();
        foreach (var sample in observation.Samples.Values)
        {
            var path20 = paths20.GetValueOrDefault(sample.Prefix);
            var path60 = paths60.GetValueOrDefault(sample.Prefix);
            var a = sample.Slow(20) && Matches(path20, sample.Window(20));
            var b = sample.Slow(60) && Matches(path60, sample.Window(60));
            var c = sample.Fast;
            if (store.Step(sample.Prefix, minute, a, b, c) && !state.Suppressed.Contains(sample.Prefix))
                findings.Add(new AbuseFinding(sample, c ? "high request volume" : a ? "steady repeated downloads" : "persistent repeated downloads", a ? path20 : b ? path60 : null));
        }
        observation.Validate(clock.GetUtcNow());
        var queued = state.Outbox.SelectMany(x => x.Subjects).ToHashSet(StringComparer.Ordinal);
        var newSubjects = findings.Select(x => x.Sample.Prefix).Where(x => !state.Subjects.Contains(x) && !queued.Contains(x)).ToHashSet(StringComparer.Ordinal);
        foreach (var finding in findings) state.Subjects.Add(finding.Sample.Prefix);
        if (newSubjects.Count > 0) Queue(newSubjects, []);
        // Queue confirmed detections before any AlertCenter query. An API outage must not erase a short event.
        store.Save();
        IReadOnlyList<AbuseImpact> impacts = [];
        var impactOk = true;
        if (state.Subjects.Count > 0 && !state.Subjects.All(x => observation.For(x).Recovered))
        {
            try { impacts = await db.AbuseImpacts(observation.Through, state.Subjects, ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (Exception ex) { impactOk = false; log.LogWarning("Abuse impact query failed ({ErrorType}); severity remains unconfirmed", ex.GetType().Name); }
        }
        if (impacts.Any(x => x.Confirmed) && state.Level != "sev2" && !state.Outbox.Any(x => store.Report(x).Level == "sev2"))
        {
            var prepared = state.Outbox.FirstOrDefault(x => !x.Started);
            if (prepared != null)
            {
                var request = store.Report(prepared);
                request.Level = "sev2";
                request.Title = Title(findings, impacts);
                request.Content = Describe(observation, findings, impacts);
                request.Metadata!["evidence"] = Evidence(observation, findings, impacts);
                prepared.Payload = JsonConvert.SerializeObject(request);
            }
            else Queue(state.Subjects.ToHashSet(StringComparer.Ordinal), impacts);
            store.Save();
        }
        observation.Validate(clock.GetUtcNow());
        var incidents = await Active(ct);
        var known = state.Incident;
        IncidentDetails? incident = known.HasValue ? (await api.GetIncidentAsync(known.Value).WaitAsync(ct)).Incident : null;
        if (incident != null) { Adopt(incident); if (Closed(incident)) incident = null; }
        if (incident == null && incidents.SingleOrDefault(x => x.IncidentId != known) is { } discovered)
        {
            var fresh = (await api.GetIncidentAsync(discovered.IncidentId).WaitAsync(ct)).Incident;
            Adopt(fresh);
            if (!Closed(fresh)) incident = fresh;
        }
        foreach (var delivery in state.Outbox.OrderByDescending(x => x.Started).ThenByDescending(x => store.Report(x).Level == "sev2").ToArray())
        {
            var report = store.Report(delivery);
            if (!delivery.Started && delivery.Subjects.All(state.Suppressed.Contains))
            {
                state.Outbox.Remove(delivery);
                store.Save();
                continue;
            }
            observation.Validate(clock.GetUtcNow());
            if (delivery.Started || incident == null)
            {
                if (!delivery.Started)
                {
                    var evidence = (JObject)report.Metadata!["evidence"]!;
                    var original = evidence["subjects"]!.ToObject<string[]>()!;
                    var retained = original.Where(x => !state.Suppressed.Contains(x)).ToArray();
                    if (retained.Length != original.Length)
                    {
                        evidence["subjects"] = JArray.FromObject(retained);
                        evidence["sources"] = new JArray(((JArray)evidence["sources"]!).Where(x => retained.Contains(x.Value<string>("prefix"))));
                        // Evidence involving an acknowledged source does not prove impact from a different source.
                        evidence["impact"] = new JArray(); report.Level = "sev3";
                    }
                    delivery.Payload = JsonConvert.SerializeObject(report);
                    delivery.Started = true;
                    store.Save();
                }
                var response = await api.CreateIncidentAsync(report).WaitAsync(ct);
                incident = (await api.GetIncidentAsync(response.IncidentId).WaitAsync(ct)).Incident;
                Adopt(incident);
                state.LastMetadata = minute;
                if (Closed(incident)) incident = null;
            }
            else
            {
                var current = (await api.GetIncidentAsync(incident.IncidentId).WaitAsync(ct)).Incident;
                Adopt(current);
                if (Closed(current)) { incident = null; continue; }
                if (report.Level == "sev2" && current.Level == "sev3")
                {
                    await api.ChangeIncidentSeverityAsync(current.IncidentId, new ChangeIncidentSeverityRequest
                    {
                        Version = current.Version, Level = "sev2", Reason = report.Content!
                    }).WaitAsync(ct);
                    current = (await api.GetIncidentAsync(current.IncidentId).WaitAsync(ct)).Incident;
                    Adopt(current);
                }
                observation.Validate(clock.GetUtcNow());
                if (Closed(current)) { incident = null; continue; }
                var evidence = (JObject)report.Metadata!["evidence"]!;
                evidence["subjects"] = JArray.FromObject(state.Subjects.Except(state.Suppressed).Order(StringComparer.Ordinal));
                await api.PatchIncidentMetadataAsync(current.IncidentId, new PatchIncidentMetadataRequest
                {
                    Version = current.Version, Property = "evidence", Value = evidence
                }).WaitAsync(ct);
                state.LastMetadata = minute;
                incident = current;
            }
            state.Outbox.Remove(delivery);
            store.Save();
        }
        var liveEvidence = Evidence(observation, findings, impacts);
        if (incident != null)
        {
            var current = (await api.GetIncidentAsync(incident.IncidentId).WaitAsync(ct)).Incident;
            Adopt(current);
            observation.Validate(clock.GetUtcNow());
            if (!Closed(current))
            {
                if (state.Subjects.Count > 0 && state.Subjects.All(x => observation.For(x).Recovered) && state.Outbox.Count == 0)
                {
                    await api.MitigateIncidentAsync(current.IncidentId, new MitigateIncidentRequest
                    {
                        Version = current.Version,
                        Reason = $"Abnormal activity on {settings.Server} has subsided. Every tracked source stayed below 12.5 kB/s and 150 requests/minute for ten complete minutes of fresh logs. This includes traffic stopping after a block; it does not verify whether a block was applied. The incident is mitigated; final resolution remains manual."
                    }).WaitAsync(ct);
                    state.Incident = null; state.Level = ""; state.Subjects.Clear(); store.BreakSequences();
                }
                else if (minute - state.LastMetadata >= 5)
                {
                    await api.PatchIncidentMetadataAsync(current.IncidentId, new PatchIncidentMetadataRequest
                    {
                        Version = current.Version, Property = "evidence", Value = liveEvidence
                    }).WaitAsync(ct);
                    state.LastMetadata = minute;
                }
            }
        }

        void Queue(HashSet<string> subjects, IReadOnlyList<AbuseImpact> impact)
        {
            var window = findings.Count == 0 ? 5 : findings.Max(x => x.Reason == "high request volume" ? 5 : x.Reason == "steady repeated downloads" ? 20 : 60);
            var report = new CreateIncidentRequest
            {
                ReportId = Guid.NewGuid(), Level = impact.Any(x => x.Confirmed) ? "sev2" : "sev3", Title = Title(findings, impact),
                Content = Describe(observation, findings, impact), OccurredAtUtc = observation.Through.AddMinutes(-window),
                Metadata = new JObject { ["source"] = settings.AbuseSource, ["server"] = settings.Server,
                    ["scope"] = store.Scope, ["kind"] = "request-abuse", ["evidence"] = Evidence(observation, findings, impact) }
            };
            state.Outbox.Add(new AbuseDelivery { Payload = JsonConvert.SerializeObject(report), Subjects = subjects });
        }
        store.Save();
        if (!ok20 || !ok60 || !impactOk) throw new InvalidOperationException("Some abuse evidence queries were unavailable.");
        log.LogInformation("Abuse scan completed for {Count} source groups on {Server}", observation.Samples.Count, settings.Server);

        async Task<(IReadOnlyDictionary<string, AbusePaths>, bool)> Paths(int length, string[] sources)
        {
            try { return (await db.AbusePaths(observation.Through, length, sources, ct), true); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
            catch (Exception ex)
            {
                log.LogWarning("Abuse path query failed ({ErrorType}); dependent confirmations discarded", ex.GetType().Name);
                return (new Dictionary<string, AbusePaths>(), false);
            }
        }
    }

    private static bool Matches(AbusePaths? paths, AbuseWindow sample) => paths is { Repeated: true } &&
        paths.Requests == sample.Requests && paths.Bytes == sample.Bytes;

    private async Task<IReadOnlyList<IncidentDetails>> Active(CancellationToken ct)
    {
        var active = new List<IncidentDetails>();
        for (var page = 1; ; page++)
        {
            if (page > 100) throw new InvalidDataException("Abuse incident pagination exceeded its bound.");
            var response = await api.QueryIncidentsAsync(new QueryIncidentsRequest
            {
                ActiveOnly = true, MetadataKey = "source", MetadataValue = settings.AbuseSource, Page = page, PageSize = 100
            }).WaitAsync(ct);
            active.AddRange(response.Items.Where(x => x.Status is not ("mitigated" or "resolved")));
            if (page * 100 >= response.TotalCount) break;
            if (response.Items.Count == 0) throw new InvalidDataException("Incomplete abuse incident pagination.");
        }
        if (active.Count > 1) throw new InvalidDataException("Multiple active abuse incidents for one server.");
        return active;
    }

    private void Adopt(IncidentDetails incident)
    {
        if (incident.Metadata.Value<string>("source") != settings.AbuseSource || incident.Metadata.Value<string>("server") != settings.Server ||
            incident.Metadata.Value<string>("scope") != store.Scope || incident.ReceivedAtUtc == default || incident.ReceivedAtUtc > clock.GetUtcNow().AddMinutes(1))
            throw new InvalidDataException("Abuse incident identity mismatch.");
        var subjects = incident.Metadata["evidence"]?["subjects"]?.ToObject<string[]>()
            ?? throw new InvalidDataException("Missing abuse incident subjects.");
        if (subjects.Length == 0 || subjects.Any(x => !AbuseSources.Included(x, settings))) throw new InvalidDataException("Invalid abuse incident subjects.");
        store.State.Incident = incident.IncidentId;
        store.State.Level = incident.Level;
        store.State.Subjects.UnionWith(subjects);
    }

    private bool Closed(IncidentDetails incident)
    {
        if (incident.Status is not ("mitigated" or "resolved")) return false;
        var subjects = incident.Metadata["evidence"]!["subjects"]!.ToObject<string[]>()!;
        store.State.Suppressed.UnionWith(subjects);
        store.State.Subjects.ExceptWith(subjects);
        store.State.Incident = null;
        store.State.Level = "";
        return true;
    }

    private JObject Evidence(AbuseObservation observation, IReadOnlyList<AbuseFinding> findings, IReadOnlyList<AbuseImpact> impacts) => new()
    {
        ["through"] = observation.Through, ["subjects"] = JArray.FromObject(store.State.Subjects.Order(StringComparer.Ordinal)),
        ["sources"] = JArray.FromObject(findings.OrderByDescending(x => x.Sample.Window(20).Bytes).Take(10).Select(x => new
        {
            prefix = x.Sample.Prefix, reason = x.Reason, twenty = x.Sample.Window(20), five = x.Sample.Window(5),
            paths = x.Paths?.Top.Select(y => new { host = Uri.CheckHostName(y.Host) == UriHostNameType.Dns ? y.Host : "(other host)",
                path = new string(y.Path.Where(c => !char.IsControl(c)).Take(240).ToArray()), y.Requests, y.Bytes })
        })),
        ["impact"] = JArray.FromObject(impacts.Where(x => x.Confirmed))
    };

    private static int WindowMinutes(AbuseFinding finding) => finding.Reason == "high request volume" ? 5 :
        finding.Reason == "steady repeated downloads" ? 20 : 60;

    private static IEnumerable<AbuseFinding> Largest(IReadOnlyList<AbuseFinding> findings) => findings
        .OrderByDescending(x => x.Sample.Window(WindowMinutes(x)).Bytes).ThenBy(x => x.Sample.Prefix, StringComparer.Ordinal);

    private string Title(IReadOnlyList<AbuseFinding> findings, IReadOnlyList<AbuseImpact> impacts)
    {
        var primary = Largest(findings).FirstOrDefault();
        var text = impacts.Any(x => x.Confirmed) ? $"HTTP activity with business impact on {settings.Server}" :
            primary?.Reason == "high request volume" ? $"Suspected request flood on {settings.Server}" :
            $"Suspected repeated downloads on {settings.Server}";
        if (primary != null)
        {
            var window = primary.Sample.Window(WindowMinutes(primary));
            text += primary.Reason == "high request volume"
                ? FormattableString.Invariant($": {window.Requests:N0} requests in 5 min from {primary.Sample.Prefix}")
                : FormattableString.Invariant($": {window.Mean / 1000:F0} kB/s from {primary.Sample.Prefix}");
        }
        if (findings.Count > 1) text += $" (+{findings.Count - 1} other sources)";
        return text[..Math.Min(200, text.Length)];
    }

    // AlertCenter renders report content as plain text, in both the details page and email.
    private string Describe(AbuseObservation observation, IReadOnlyList<AbuseFinding> findings, IReadOnlyList<AbuseImpact> impacts)
    {
        var confirmed = impacts.Where(x => x.Confirmed).ToArray();
        var text = new StringBuilder();
        text.AppendLine(confirmed.Length > 0
            ? $"HTTP activity on {settings.Server} coincides with service errors or slower responses for other visitors (sev2)."
            : $"Suspected abnormal HTTP activity on {settings.Server} (sev3). Business impact has not been confirmed.");
        if (Largest(findings).FirstOrDefault() is { } primary)
        {
            var window = primary.Sample.Window(WindowMinutes(primary));
            text.AppendLine(FormattableString.Invariant($"Main source: {primary.Sample.Prefix}; {window.Requests:N0} requests and {window.Bytes / 1_000_000.0:F1} MB sent in {window.Minutes} min ({window.Mean / 1000:F1} kB/s average)."));
        }
        text.AppendLine("This observation does not establish a successful compromise or current bandwidth saturation.");
        text.AppendLine();
        text.AppendLine("What to check");
        text.AppendLine($"Open {settings.Server}'s Grafana, Traffic V2, select Internet and the UTC time range below. Inspect the listed source addresses, sites and paths in the request logs. Check page speed and service errors before deciding whether to rate-limit or block it.");
        text.AppendLine("If review confirms abuse, manually block the source IP/CIDR using existing Clean Traffic at the affected public ingress. Guide: https://gitlab.aiursoft.com/anduin/clean-traffic");
        text.AppendLine("For persistent blocking, submit a reviewed MR adding the source to list in https://gitlab.aiursoft.com/anduin/ShameList-HackersIPs . Do not edit the GitHub mirror directly. After merge, verify GitMirror publication, run sudo -n /etc/cron.daily/auto-blacklist-update on the affected ingress, and verify set membership and firewall hits. Temporary ipset additions can be overwritten by refresh.");
        text.AppendLine("Recovery: automatically mitigate after every tracked source stays below 12.5 kB/s and 150 requests/minute for ten fresh complete minutes, including after blocking. Missing logs do not count as recovery. Final resolution is manual.");
        text.AppendLine();
        text.AppendLine("Observed activity (UTC; window start is not the exact attack onset)");
        foreach (var finding in Largest(findings).Take(2))
        {
            var window = finding.Sample.Window(WindowMinutes(finding));
            var total = observation.Samples.Values.Sum(x => (double)x.Window(window.Minutes).Bytes);
            var share = total > 0 ? FormattableString.Invariant($"; {100.0 * window.Bytes / total:F1}% of monitored public response bytes") : "";
            var behavior = finding.Reason == "high request volume" ? "high request volume" :
                finding.Reason == "steady repeated downloads" ? "repeated downloads with little variation" : "persistent repeated downloads";
            text.AppendLine(FormattableString.Invariant($"{finding.Sample.Prefix}: {behavior}."));
            text.AppendLine(FormattableString.Invariant($"  {observation.Through.AddMinutes(-window.Minutes):yyyy-MM-dd HH:mm} to {observation.Through:yyyy-MM-dd HH:mm} UTC ({window.Minutes} min): {window.Mean / 1000:F1} kB/s average, {window.Bytes / 1_000_000.0:F1} MB sent, {window.Requests:N0} requests{share}."));
            if (finding.Reason != "high request volume")
                text.AppendLine($"  Sustained activity in {window.ActiveMinutes}/{window.Minutes} minutes; repeated paths ignore query strings.");
            if (finding.Paths is { } paths)
            {
                text.AppendLine($"  Leading paths by response bytes ({paths.Distinct} distinct site/path pairs):");
                foreach (var path in paths.Top.OrderByDescending(x => x.Bytes).Take(2))
                    text.AppendLine(FormattableString.Invariant($"    {Display(path.Host, 60)}{Display(path.Path.Split('?')[0], 80)}: {path.Requests:N0} requests, {path.Bytes / 1_000_000.0:F1} MB"));
            }
            else text.AppendLine("  Leading paths were not collected for this request-volume finding; inspect the source in Traffic V2.");
        }
        if (findings.Count > 2) text.AppendLine($"{findings.Count - 2} additional sources are listed in incident metadata > evidence.");
        if (findings.Count == 0) text.AppendLine($"No newly confirmed source pattern in this scan. Observation through {observation.Through:yyyy-MM-dd HH:mm} UTC; tracked sources remain in incident metadata > evidence.");
        if (confirmed.Length > 0)
        {
            text.AppendLine();
            text.AppendLine("Business impact evidence (last 5 minutes)");
            foreach (var impact in confirmed.Take(2))
            {
                text.AppendLine(FormattableString.Invariant($"{Display(impact.Host, 80)}: {impact.SourceErrors:N0}/{impact.SourceRequests:N0} requests from tracked sources returned 5xx; these account for {(impact.AllErrors > 0 ? 100.0 * impact.SourceErrors / impact.AllErrors : 0):F1}% of this site's 5xx."));
                if (impact.OtherRequests >= 20 && impact.BaselineRequests >= 20 && impact.BaselineP95 > 0)
                    text.AppendLine(FormattableString.Invariant($"  Other visitors' p95 response time: {impact.OtherP95 / 1000:F2} s, versus {impact.BaselineP95 / 1000:F2} s baseline ({impact.OtherRequests}/{impact.BaselineRequests} samples)."));
            }
        }
        text.AppendLine();
        text.AppendLine("Technical evidence: incident metadata > evidence contains source groups, paths, errors and impact measurements. kB/MB use decimal units. Public response share excludes private/reserved and allowlisted sources; it is not a percentage of link capacity.");
        var content = text.ToString().TrimEnd();
        // Keep the useful summary and action first even with long paths or multiple sources.
        return content.Length <= 4000 ? content : content[..content.LastIndexOf('\n', 3900)] +
            "\nFurther evidence is available in incident metadata > evidence.";
    }

    private static string Display(string value, int limit)
    {
        var clean = new string(value.Where(c => !char.IsControl(c) && char.GetUnicodeCategory(c) !=
            System.Globalization.UnicodeCategory.Format).ToArray());
        return clean.Length <= limit ? clean : clean[..limit] + "…";
    }

}

internal sealed class AbuseScanner(IServiceScopeFactory scopes, AbuseStore store, TimeProvider clock, ILogger<AbuseScanner> log) : BackgroundService
{
    internal const string HealthFile = "/tmp/http-abuse-bridge-health";
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            var minute = clock.GetUtcNow().ToUnixTimeSeconds() / 60;
            var cutoff = DateTimeOffset.FromUnixTimeSeconds((minute - 1) * 60);
            using var budget = CancellationTokenSource.CreateLinkedTokenSource(stoppingToken);
            budget.CancelAfter(TimeSpan.FromSeconds(55));
            try
            {
                await using var scope = scopes.CreateAsyncScope();
                var observation = await scope.ServiceProvider.GetRequiredService<RequestLog>().AbuseRecent(cutoff, budget.Token);
                await scope.ServiceProvider.GetRequiredService<AbuseReporter>().Scan(observation, minute, budget.Token);
                await File.WriteAllTextAsync(HealthFile, clock.GetUtcNow().ToString("O"), stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                store.BreakSequences();
                log.LogWarning("Abuse scan failed ({ErrorType}); no recovery evidence assumed", ex.GetType().Name);
            }
            var next = DateTimeOffset.FromUnixTimeSeconds((clock.GetUtcNow().ToUnixTimeSeconds() / 60 + 1) * 60);
            var delay = next - clock.GetUtcNow();
            if (delay > TimeSpan.Zero) await Task.Delay(delay, stoppingToken);
        }
    }
}
