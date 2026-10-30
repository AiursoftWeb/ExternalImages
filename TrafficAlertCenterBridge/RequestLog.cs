using Aiursoft.ClickhouseSdk;
using Aiursoft.ClickhouseSdk.Abstractions;
using System.Globalization;

namespace TrafficBridge;

internal sealed class RequestLog(Settings settings) : ClickhouseDbContext(new ClickhouseOptions
{
    Enabled = true, ConnectionString = settings.ConnectionString
})
{
    public override Task SaveChangesAsync() => throw new NotSupportedException("Request logs are read-only.");

    internal async Task<Observation> Read(DateTimeOffset cutoff, CancellationToken ct)
    {
        var parameters = new Dictionary<string, object?>
        {
            ["from"] = cutoff.AddMinutes(-10).UtcDateTime, ["through"] = cutoff.UtcDateTime,
            ["latest"] = cutoff.AddMinutes(1).UtcDateTime, ["hosts"] = settings.Hosts, ["all"] = (byte)(settings.AllHosts ? 1 : 0)
        };
        var coverage = await QueryAsync("""
            SELECT toUnixTimestamp(toStartOfMinute(ts)) FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {latest:DateTime}
            GROUP BY 1
            SETTINGS readonly=1, max_threads=1, max_execution_time=15, max_memory_usage=600000000
            """, r => Convert.ToInt64(r.GetValue(0), CultureInfo.InvariantCulture), parameters).WaitAsync(ct);
        // Global source activity, including internal probes, distinguishes quiet public traffic from missing collection.
        if (Enumerable.Range(0, 11).Any(j => !coverage.Contains(cutoff.ToUnixTimeSeconds() - (10 - j) * 60)))
            throw new InvalidDataException("Request log collection has missing or stale minutes.");
        var rows = await QueryAsync("""
            SELECT toUnixTimestamp(toStartOfMinute(ts)),
                   if(position(remote_ip, ':') > 0,
                      concat(IPv6NumToString(tupleElement(IPv6CIDRToRange(toIPv6OrDefault(remote_ip), 64), 1)), '/64'), remote_ip),
                   lower(splitByChar(':', host)[1]), sum(bytes_sent),
                   sumIf(bytes_sent, match(splitByChar('?', uri)[1], '(?i)[.](css|js|png|jpg|jpeg|gif|svg|webp|ico|woff2?)(/|$)'))
            FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {through:DateTime} AND status != 101
              AND ({all:UInt8} = 1 OR lower(splitByChar(':', host)[1]) IN {hosts:Array(String)})
            GROUP BY 1, 2, 3
            SETTINGS readonly=1, max_threads=1, max_execution_time=30, max_memory_usage=600000000,
                     max_result_rows=100000, result_overflow_mode='throw'
            """, r => new Bucket(Convert.ToInt64(r.GetValue(0), CultureInfo.InvariantCulture), r.GetString(1), r.GetString(2),
                Convert.ToUInt64(r.GetValue(3), CultureInfo.InvariantCulture), Convert.ToUInt64(r.GetValue(4), CultureInfo.InvariantCulture)), parameters).WaitAsync(ct);
        if (rows.Any(x => x.Minute < cutoff.AddMinutes(-10).ToUnixTimeSeconds() || x.Minute >= cutoff.ToUnixTimeSeconds() || x.StaticBytes > x.Bytes))
            throw new InvalidDataException("Invalid request log buckets.");
        return new Observation(cutoff, rows.Where(x => Sources.Included(x.Prefix, settings))
            .Select(x => x with { Host = Uri.CheckHostName(x.Host) == UriHostNameType.Dns ? x.Host : "(other host)" }).ToArray());
    }
}
