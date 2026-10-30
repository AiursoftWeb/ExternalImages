using System.Net;
using System.Net.Sockets;

namespace TrafficBridge;

internal sealed record Bucket(long Minute, string Prefix, string Host, ulong Bytes, ulong StaticBytes);
internal sealed record SourceSample(string Prefix, ulong[] Minutes, ulong TotalBytes, ulong StaticBytes, string[] TopHosts)
{
    internal double BytesPerSecond => Minutes.Sum(x => (double)x) / 600;
    internal double Share => TotalBytes == 0 ? 0 : Minutes.Sum(x => (double)x) / TotalBytes;
    internal bool Qualifies => BytesPerSecond >= 150_000 && Share >= 0.60 &&
        Minutes.Count(x => x >= 75_000 * 60UL) >= 8 && Minutes[^1] >= 75_000 * 60UL;
    internal bool Recovered => Minutes.TakeLast(5).All(x => x < 75_000 * 60UL);
}

internal sealed record Observation(DateTimeOffset Through, IReadOnlyList<Bucket> Buckets)
{
    private Dictionary<string, Bucket[]>? _bySource;
    private ulong? _total;

    internal SourceSample For(string prefix)
    {
        _bySource ??= Buckets.GroupBy(x => x.Prefix).ToDictionary(x => x.Key, x => x.ToArray(), StringComparer.Ordinal);
        _total ??= Buckets.Aggregate(0UL, (sum, x) => checked(sum + x.Bytes));
        var rows = _bySource.GetValueOrDefault(prefix) ?? [];
        var minutes = Enumerable.Range(0, 10).Select(j => rows.Where(x => x.Minute == Through.ToUnixTimeSeconds() - (10 - j) * 60)
            .Aggregate(0UL, (sum, x) => checked(sum + x.Bytes))).ToArray();
        return new SourceSample(prefix, minutes, _total.Value,
            rows.Aggregate(0UL, (sum, x) => checked(sum + x.StaticBytes)), rows.GroupBy(x => x.Host)
                .OrderByDescending(x => x.Sum(y => (double)y.Bytes)).Take(3).Select(x => x.Key).ToArray());
    }
}

internal static class Sources
{
    private static readonly IPNetwork[] Private = new[]
    {
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/3", "::/96", "::ffff:0:0/96", "64:ff9b::/96", "100::/64", "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8"
    }.Select(IPNetwork.Parse).ToArray();

    internal static bool Included(string prefix, Settings settings)
    {
        if (!IPAddress.TryParse(prefix.Split('/')[0], out var address)) return false;
        return address.AddressFamily is AddressFamily.InterNetwork or AddressFamily.InterNetworkV6 &&
            !Private.Concat(settings.ExcludedSources).Any(x => x.Contains(address));
    }
}

internal sealed class Sequence
{
    private long _last = -1;
    private int _count;
    internal bool Step(long minute, bool matches, int required)
    {
        if (minute == _last) return matches && _count >= required;
        _count = matches ? (_last == minute - 1 ? Math.Min(_count + 1, required) : 1) : 0;
        _last = minute;
        return _count >= required;
    }
}

internal sealed class Evidence
{
    internal Sequence Failure { get; } = new();
    internal Sequence Healthy { get; } = new();
    internal long LastUpdate { get; set; } = -1;
    internal Guid? Incident { get; set; }
}
