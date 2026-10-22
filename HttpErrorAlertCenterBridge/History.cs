using System.Text.Json;

namespace HttpErrorBridge;

internal sealed class History(Settings settings)
{
    internal const int Capacity = 8640;
    private HistoryData? _data;
    private long? _baselineThrough;
    private readonly Dictionary<string, Baseline?> _baselines = new(StringComparer.Ordinal);
    private string PathName => Path.Combine(settings.DataDirectory, "history.json");

    internal async Task Refresh(RequestLog db, DateTimeOffset cutoff, CancellationToken ct)
    {
        var day = new DateTimeOffset(cutoff.UtcDateTime.Date, TimeSpan.Zero);
        var through = day.ToUnixTimeSeconds();
        var scope = string.Join(",", settings.Hosts);
        _data ??= File.Exists(PathName)
            ? JsonSerializer.Deserialize<HistoryData>(await File.ReadAllBytesAsync(PathName, ct))
                ?? throw new InvalidDataException("Invalid history cache.")
            : new HistoryData();
        if (_data.Scope != scope || _data.Through < through - 30 * 86400L)
            _data = new HistoryData { Scope = scope };
        if (_data.Through % 86400 != 0) throw new InvalidDataException("Invalid history boundary.");
        if (_data.Through > through) throw new InvalidDataException("History cache is ahead of the clock.");
        if (_baselineThrough == through) return;
        var from = Math.Max(_data.Through, through - 30 * 86400L);
        var rows = from < through
            ? await db.Read(DateTimeOffset.FromUnixTimeSeconds(from), day, 300, ct)
            : [];
        foreach (var (host, bins) in _data.Hosts.ToArray())
        {
            if (bins.Values.Any(x => x.Errors > x.Requests)) throw new InvalidDataException("Invalid history counts.");
            foreach (var slot in bins.Keys.Where(x => x < through / 300 - Capacity || x >= from / 300).ToArray()) bins.Remove(slot);
            if (bins.Count == 0) _data.Hosts.Remove(host);
        }
        foreach (var row in rows)
        {
            if (!_data.Hosts.TryGetValue(row.Host, out var bins)) _data.Hosts[row.Host] = bins = [];
            bins[row.Start / 300] = row.Counts;
        }
        _data.Through = through;
        Directory.CreateDirectory(settings.DataDirectory);
        StateStore.AtomicWrite(PathName, _data);
        _baselines.Clear();
        foreach (var (host, bins) in _data.Hosts) _baselines[host] = Calculate(bins, day);
        _baselineThrough = through;
    }

    internal Baseline? For(string host) => _baselines.GetValueOrDefault(host);

    internal static Baseline? Calculate(IReadOnlyDictionary<long, Counts> bins, DateTimeOffset day)
    {
        var end = day.ToUnixTimeSeconds() / 300;
        var slots = new HashSet<long>();
        // Only windows touching a nonempty bucket can contribute a historical rate.
        foreach (var bucket in bins.Keys)
            for (var slot = bucket; slot <= bucket + 2; slot++)
                if (slot >= end - Capacity + 2 && slot < end) slots.Add(slot);
        var values = new List<double>(slots.Count);
        var days = new HashSet<long>();
        foreach (var slot in slots)
        {
            var count = bins.GetValueOrDefault(slot) + bins.GetValueOrDefault(slot - 1) + bins.GetValueOrDefault(slot - 2);
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
        public string Scope { get; set; } = "";
        public long Through { get; set; }
        public Dictionary<string, Dictionary<long, Counts>> Hosts { get; set; } = new(StringComparer.Ordinal);
    }
}
