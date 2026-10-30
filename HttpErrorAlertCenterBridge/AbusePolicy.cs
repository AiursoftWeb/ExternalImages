using System.Net;
using System.Net.Sockets;

namespace HttpErrorBridge;

internal sealed record AbuseMinute(long Start, ulong Requests, ulong Bytes, int ActiveSlots, ulong Errors);
internal sealed record AbusePaths(ulong Requests, ulong Bytes, int Distinct, IReadOnlyList<AbusePath> Top)
{
    internal bool Repeated => Requests > 0 && Bytes > 0 &&
        Top.Sum(x => (double)x.Bytes) / Bytes >= 0.90 && 1 - (double)Distinct / Requests >= 0.85;
}
internal sealed record AbusePath(string Host, string Path, ulong Requests, ulong Bytes);
internal sealed record AbuseWindow(int Minutes, ulong Requests, ulong Bytes, double Mean, double Cv,
    int ActiveMinutes, double Activity, double LatestMean, ulong Errors, ulong LatestRequests);

internal sealed record AbuseSample(string Prefix, DateTimeOffset Through, IReadOnlyList<AbuseMinute> Rows)
{
    internal AbuseWindow Window(int length)
    {
        var byMinute = Rows.ToDictionary(x => x.Start);
        var values = Enumerable.Range(0, length).Select(j => byMinute.GetValueOrDefault(
            Through.ToUnixTimeSeconds() - (length - j) * 60) ?? new AbuseMinute(0, 0, 0, 0, 0)).ToArray();
        var bytes = values.Aggregate(0UL, (a, x) => checked(a + x.Bytes));
        var mean = (double)bytes / length;
        var cv = mean > 0 ? Math.Sqrt(values.Sum(x => Math.Pow(x.Bytes - mean, 2)) / length) / mean : 0;
        return new AbuseWindow(length, values.Aggregate(0UL, (a, x) => checked(a + x.Requests)), bytes,
            mean / 60, cv, values.Count(x => x.Requests >= 3 && x.Bytes >= 300_000),
            values.Sum(x => x.ActiveSlots) / (length * 6.0), values.TakeLast(3).Sum(x => (double)x.Bytes) / 180,
            values.Aggregate(0UL, (a, x) => checked(a + x.Errors)), values.TakeLast(2).Aggregate(0UL, (a, x) => checked(a + x.Requests)));
    }

    internal bool Slow(int length)
    {
        var w = Window(length);
        return w.Mean >= 25_000 && w.Requests >= (ulong)length * 6 && w.ActiveMinutes >= length * 0.90 &&
            w.Activity >= 0.80 && w.LatestMean >= 25_000 && (length == 60 || w.Cv <= 0.50);
    }

    internal bool Fast
    {
        get
        {
            var w = Window(5);
            return w.Requests >= 3_000 && (w.Requests >= 10_000 || w.Errors >= 100) && w.LatestRequests >= 300 &&
                Rows.Count(x => x.Start >= Through.AddMinutes(-5).ToUnixTimeSeconds() && x.Requests >= 300) >= 2;
        }
    }

    internal bool Recovered => Enumerable.Range(1, 10).All(j =>
    {
        var row = Rows.SingleOrDefault(x => x.Start == Through.ToUnixTimeSeconds() - j * 60);
        return row == null || row.Bytes < 750_000 && row.Requests < 150;
    });
}

internal sealed record AbuseObservation(DateTimeOffset Through, DateTimeOffset Latest, IReadOnlyDictionary<string, AbuseSample> Samples)
{
    internal AbuseSample For(string prefix) => Samples.GetValueOrDefault(prefix) ?? new AbuseSample(prefix, Through, []);
    internal void Validate(DateTimeOffset now)
    {
        if (now < Through || now - Through > TimeSpan.FromMinutes(3) || Latest < Through || Latest >= Through.AddMinutes(1))
            throw new InvalidDataException("Abuse observation is stale or incomplete.");
    }
}

internal static class AbuseSources
{
    private static readonly IPNetwork[] Private = new[]
    {
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
        "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/3", "::/96", "::ffff:0:0/96", "64:ff9b::/96", "100::/64", "2001:db8::/32", "fc00::/7", "fe80::/10", "ff00::/8"
    }.Select(IPNetwork.Parse).ToArray();

    internal static bool Included(string prefix, Settings settings)
    {
        if (!IPAddress.TryParse(prefix.Split('/')[0], out var ip)) return false;
        if (ip.AddressFamily == AddressFamily.InterNetwork && prefix != ip.ToString()) return false;
        if (ip.AddressFamily == AddressFamily.InterNetworkV6 &&
            (!IPNetwork.TryParse(prefix, out var network) || network.PrefixLength != 64 || prefix != network.ToString())) return false;
        return ip.AddressFamily is AddressFamily.InterNetwork or AddressFamily.InterNetworkV6 &&
            !Private.Concat(settings.AbuseExcludedSources).Any(x => x.Contains(ip));
    }
}

internal sealed record AbuseImpact(string Host, ulong SourceRequests, ulong SourceErrors, ulong AllErrors,
    ulong OtherRequests, double OtherP95, ulong BaselineRequests, double BaselineP95, double SourceShare, ulong LatestSourceRequests, ulong LatestSourceErrors)
{
    internal bool Confirmed => LatestSourceRequests > 0 && (LatestSourceErrors > 0 && SourceErrors >= 100 && AllErrors > 0 && (double)SourceErrors / AllErrors >= 0.50 &&
        SourceRequests > 0 && (double)SourceErrors / SourceRequests >= 0.05 ||
        SourceShare >= 0.60 && OtherRequests >= 20 && BaselineRequests >= 20 && BaselineP95 > 0 &&
        OtherP95 >= 2_000 && OtherP95 >= BaselineP95 * 3);
}
