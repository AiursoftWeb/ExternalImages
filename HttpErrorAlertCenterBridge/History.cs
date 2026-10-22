using System.Text.Json;

namespace HttpErrorBridge;

internal sealed class History(Settings settings)
{
    internal const int Capacity = 8640;
    private HistoryData? _data;
    private readonly Dictionary<string, Baseline?> _baselines = new(StringComparer.Ordinal);
    private string PathName => Path.Combine(settings.DataDirectory, "history.json");

    internal async Task Refresh(RequestLog db, DateTimeOffset cutoff, CancellationToken ct)
    {
        var day = new DateTimeOffset(cutoff.UtcDateTime.Date, TimeSpan.Zero);
        _data ??= File.Exists(PathName)
            ? JsonSerializer.Deserialize<HistoryData>(await File.ReadAllBytesAsync(PathName, ct))
                ?? throw new InvalidDataException("Invalid history cache.")
            : new HistoryData();
        var through = day.ToUnixTimeSeconds();
        if (_data.Through % 86400 != 0) throw new InvalidDataException("Invalid history boundary.");
        if (_data.Through > through) throw new InvalidDataException("History cache is ahead of the clock.");
        var sameHosts = _data.Hosts.Count == settings.Hosts.Length && settings.Hosts.All(_data.Hosts.ContainsKey);
        if (_data.Through == through && sameHosts && _baselines.Count == settings.Hosts.Length) return;
        if (!sameHosts || _data.Through < through - 30 * 86400L)
            _data = new HistoryData();
        var from = Math.Max(_data.Through, through - 30 * 86400L);
        foreach (var host in settings.Hosts)
        {
            if (!_data.Hosts.TryGetValue(host, out var bins))
                _data.Hosts[host] = bins = new Counts[Capacity];
            if (bins.Length != Capacity || bins.Any(x => x.Errors > x.Requests)) throw new InvalidDataException("Invalid history capacity.");
            // Advance the ring, clearing overwritten days. Persist only after a complete query.
            for (var slot = from / 300; slot < through / 300; slot++) bins[slot % Capacity] = default;
        }
        if (from < through)
        {
            var rows = await db.Read(DateTimeOffset.FromUnixTimeSeconds(from), day, 300, ct);
            foreach (var row in rows) _data.Hosts[row.Host][row.Start / 300 % Capacity] = row.Counts;
        }
        _data.Through = through;
        Directory.CreateDirectory(settings.DataDirectory);
        StateStore.AtomicWrite(PathName, _data);
        _baselines.Clear();
        foreach (var host in settings.Hosts) _baselines[host] = Calculate(_data.Hosts[host], day);
    }

    internal Baseline? For(string host) => _baselines.GetValueOrDefault(host);

    internal static Baseline? Calculate(Counts[] bins, DateTimeOffset day)
    {
        var end = day.ToUnixTimeSeconds() / 300;
        var values = new List<double>(Capacity);
        var days = new HashSet<long>();
        // Three adjacent five-minute buckets form each historical fifteen-minute sample.
        for (var slot = end - Capacity + 2; slot < end; slot++)
        {
            var count = bins[slot % Capacity] + bins[(slot - 1) % Capacity] + bins[(slot - 2) % Capacity];
            if (count.Rate is not { } rate) continue;
            values.Add(rate);
            days.Add((slot * 300 + 299) / 86400);
        }
        if (days.Count < 7) return null;
        values.Sort();
        return new Baseline(values[(int)Math.Ceiling(0.95 * values.Count) - 1],
            values[(int)Math.Ceiling(0.99 * values.Count) - 1], values.Count, days.Count, day);
    }

    internal sealed class HistoryData
    {
        public long Through { get; set; }
        public Dictionary<string, Counts[]> Hosts { get; set; } = new(StringComparer.Ordinal);
    }
}
