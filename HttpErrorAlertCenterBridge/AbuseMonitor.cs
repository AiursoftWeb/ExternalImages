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
                        Reason = "Ten fresh complete minutes below 12500 B/s and 150 requests/minute for every implicated source. Stopped traffic, including after blocking, is recovery. Final resolution remains manual."
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
                ReportId = Guid.NewGuid(), Level = impact.Any(x => x.Confirmed) ? "sev2" : "sev3", Title = $"Abnormal HTTP requests on {settings.Server}",
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

    private string Describe(AbuseObservation observation, IReadOnlyList<AbuseFinding> findings, IReadOnlyList<AbuseImpact> impacts)
    {
        var sources = findings.Take(10).Select(x => FormattableString.Invariant(
            $"{x.Sample.Prefix}: {x.Reason}; 20-minute mean {x.Sample.Window(20).Mean:F0} B/s, CV {x.Sample.Window(20).Cv:F3}; 5-minute requests {x.Sample.Window(5).Requests}, 5xx {x.Sample.Window(5).Errors}."));
        var confirmed = impacts.Where(x => x.Confirmed).Select(x => FormattableString.Invariant(
            $"{x.Host}: source 5xx {x.SourceErrors}/{x.SourceRequests}; other visitors p95 {x.OtherP95:F0} ms versus {x.BaselineP95:F0} ms baseline ({x.OtherRequests}/{x.BaselineRequests} samples)."));
        return $"Server: {settings.Server}\nObservation through (UTC): {observation.Through:O}\n" + string.Join('\n', sources) +
            "\nBusiness impact: " + (confirmed.Any() ? string.Join('\n', confirmed) : "not confirmed; reported as suspected abnormal activity") +
            "\nIPv4 hosts and IPv6 /64 groups exclude private/reserved and explicitly allowlisted sources. Paths ignore query strings. " +
            "Traffic patterns do not prove an actor's identity or a successful compromise. Automated recovery mitigates; final resolution is manual.";
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
