using Aiursoft.ClickhouseSdk;
using Aiursoft.ClickhouseSdk.Abstractions;
using System.Globalization;

namespace HttpErrorBridge;

internal sealed record Bucket(string Host, long Start, Counts Counts);
internal sealed record RecentLog(DateTimeOffset Latest, IReadOnlyList<Bucket> Buckets);

internal sealed class RequestLog(Settings settings) : ClickhouseDbContext(new ClickhouseOptions
{
    Enabled = true, ConnectionString = settings.ConnectionString
})
{
    public override Task SaveChangesAsync() => throw new NotSupportedException("Request logs are read-only.");

    internal async Task<IReadOnlyList<Bucket>> Read(DateTimeOffset from, DateTimeOffset through, int seconds, CancellationToken ct)
    {
        var rows = await QueryAsync($$"""
            SELECT lower(splitByChar(':', host)[1]), toUnixTimestamp(toStartOfInterval(ts, INTERVAL {{seconds}} SECOND)),
                   count(), countIf(status >= 500 AND status < 600)
            FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {through:DateTime}
              AND ({all:UInt8} = 1 OR lower(splitByChar(':', host)[1]) IN {hosts:Array(String)}) AND status != 101
            GROUP BY lower(splitByChar(':', host)[1]), 2
            SETTINGS readonly=1, max_threads=1, max_execution_time=45, max_memory_usage=600000000
            """, r => new Bucket(r.GetString(0), Convert.ToInt64(r.GetValue(1), CultureInfo.InvariantCulture),
                new Counts(Convert.ToUInt64(r.GetValue(2), CultureInfo.InvariantCulture), Convert.ToUInt64(r.GetValue(3), CultureInfo.InvariantCulture))),
            new Dictionary<string, object?> { ["from"] = from.UtcDateTime, ["through"] = through.UtcDateTime, ["hosts"] = settings.Hosts, ["all"] = (byte)(settings.AllHosts ? 1 : 0) }).WaitAsync(ct);
        if (rows.Any(x => x.Counts.Errors > x.Counts.Requests || x.Start < from.ToUnixTimeSeconds() || x.Start >= through.ToUnixTimeSeconds()))
            throw new InvalidDataException("Invalid request log buckets.");
        return rows.Where(x => Uri.CheckHostName(x.Host) == UriHostNameType.Dns).ToArray();
    }

    internal async Task<RecentLog> Recent(DateTimeOffset cutoff, CancellationToken ct)
    {
        var latest = await QueryAsync("""
            SELECT toUnixTimestamp(max(ts)) FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {through:DateTime}
            SETTINGS readonly=1, max_threads=1, max_execution_time=15
            """, r => Convert.ToInt64(r.GetValue(0), CultureInfo.InvariantCulture),
            new Dictionary<string, object?> { ["from"] = cutoff.AddMinutes(-3).UtcDateTime, ["through"] = cutoff.AddMinutes(1).UtcDateTime }).WaitAsync(ct);
        var buckets = await Read(cutoff.AddMinutes(-15), cutoff, 60, ct);
        return new RecentLog(DateTimeOffset.FromUnixTimeSeconds(latest.Single()), buckets);
    }
}
