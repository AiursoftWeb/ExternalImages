using System.Globalization;

namespace HttpErrorBridge;

internal sealed partial class RequestLog
{
    // Caddy writes canonical IP strings; keep IPv4-mapped IPv6 clients in their IPv4 group.
    private const string AbuseAddressSql = "if(startsWith(lower(remote_ip), '::ffff:') AND isIPv4String(substring(remote_ip, 8)), substring(remote_ip, 8), remote_ip)";
    private static readonly string AbusePrefixSql = $"if(position({AbuseAddressSql}, ':') > 0, concat(IPv6NumToString(tupleElement(IPv6CIDRToRange(toIPv6OrDefault({AbuseAddressSql}), 64), 1)), '/64'), {AbuseAddressSql})";

    private Dictionary<string, object?> AbuseParameters(DateTimeOffset through, int minutes, IReadOnlyCollection<string>? prefixes = null) => new()
    {
        ["from"] = through.AddMinutes(-minutes).UtcDateTime, ["through"] = through.UtcDateTime,
        ["latest"] = through.AddMinutes(1).UtcDateTime, ["five"] = through.AddMinutes(-5).UtcDateTime,
        ["two"] = through.AddMinutes(-2).UtcDateTime, ["baselineEnd"] = through.AddMinutes(-10).UtcDateTime,
        ["hosts"] = settings.Hosts, ["all"] = (byte)(settings.AllHosts ? 1 : 0), ["prefixes"] = prefixes?.ToArray() ?? []
    };

    internal async Task<AbuseObservation> AbuseRecent(DateTimeOffset through, CancellationToken ct)
    {
        var parameters = AbuseParameters(through, 60);
        var latest = await QueryAsync("""
            SELECT toUnixTimestamp(max(ts)) FROM logs.caddy_requests
            WHERE ts >= {through:DateTime} AND ts < {latest:DateTime}
            SETTINGS readonly=1, max_threads=1, max_execution_time=15
            """, r => Convert.ToInt64(r.GetValue(0), CultureInfo.InvariantCulture), parameters).WaitAsync(ct);
        var rows = await QueryAsync($$"""
            SELECT toUnixTimestamp(toStartOfMinute(ts)), {{AbusePrefixSql}}, count(), sum(bytes_sent),
                   uniqExact(toStartOfInterval(ts, INTERVAL 10 SECOND)), countIf(status >= 500 AND status < 600)
            FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {through:DateTime} AND status != 101
              AND ({all:UInt8} = 1 OR lower(splitByChar(':', host)[1]) IN {hosts:Array(String)})
            GROUP BY 1, 2
            SETTINGS readonly=1, max_threads=1, max_execution_time=30, max_memory_usage=600000000,
                     max_result_rows=100000, result_overflow_mode='throw'
            """, r => (Prefix: r.GetString(1), Minute: new AbuseMinute(
                Convert.ToInt64(r.GetValue(0), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(2), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(3), CultureInfo.InvariantCulture),
                Convert.ToInt32(r.GetValue(4), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(5), CultureInfo.InvariantCulture))), parameters).WaitAsync(ct);
        if (rows.Any(x => x.Minute.Start < through.AddMinutes(-60).ToUnixTimeSeconds() || x.Minute.Start >= through.ToUnixTimeSeconds() ||
            x.Minute.Start % 60 != 0 || x.Minute.Errors > x.Minute.Requests || x.Minute.ActiveSlots is < 1 or > 6))
            throw new InvalidDataException("Invalid abuse minute buckets.");
        var samples = rows.Where(x => AbuseSources.Included(x.Prefix, settings)).GroupBy(x => x.Prefix)
            .ToDictionary(x => x.Key, x => new AbuseSample(x.Key, through, x.Select(y => y.Minute).ToArray()));
        return new AbuseObservation(through, DateTimeOffset.FromUnixTimeSeconds(latest.Single()), samples);
    }

    internal async Task<IReadOnlyDictionary<string, AbusePaths>> AbusePaths(DateTimeOffset through, int minutes,
        IReadOnlyCollection<string> prefixes, CancellationToken ct)
    {
        if (prefixes.Count == 0) return new Dictionary<string, AbusePaths>();
        var rows = await QueryAsync($$"""
            SELECT prefix, hostname, path, requests, bytes, total_requests, total_bytes, distinct_paths FROM
            (
                SELECT *, sum(requests) OVER (PARTITION BY prefix) total_requests,
                       sum(bytes) OVER (PARTITION BY prefix) total_bytes,
                       count() OVER (PARTITION BY prefix) distinct_paths,
                       row_number() OVER (PARTITION BY prefix ORDER BY bytes DESC, hostname, path) rank
                FROM
                (
                    SELECT {{AbusePrefixSql}} prefix, lower(splitByChar(':', host)[1]) hostname,
                           splitByChar('?', uri)[1] path, count() requests, sum(bytes_sent) bytes
                    FROM logs.caddy_requests
                    WHERE ts >= {from:DateTime} AND ts < {through:DateTime} AND status != 101
                      AND ({all:UInt8} = 1 OR lower(splitByChar(':', host)[1]) IN {hosts:Array(String)})
                      AND {{AbusePrefixSql}} IN {prefixes:Array(String)}
                    GROUP BY prefix, hostname, path
                )
            ) WHERE rank <= 10
            ORDER BY prefix, rank
            SETTINGS readonly=1, max_threads=1, max_execution_time=30, max_memory_usage=600000000,
                     max_result_rows=100000, result_overflow_mode='throw'
            """, r => (Prefix: r.GetString(0), Path: new AbusePath(r.GetString(1), r.GetString(2),
                Convert.ToUInt64(r.GetValue(3), CultureInfo.InvariantCulture), Convert.ToUInt64(r.GetValue(4), CultureInfo.InvariantCulture)),
                Requests: Convert.ToUInt64(r.GetValue(5), CultureInfo.InvariantCulture), Bytes: Convert.ToUInt64(r.GetValue(6), CultureInfo.InvariantCulture),
                Distinct: Convert.ToInt32(r.GetValue(7), CultureInfo.InvariantCulture)), AbuseParameters(through, minutes, prefixes)).WaitAsync(ct);
        if (rows.Any(x => !prefixes.Contains(x.Prefix) || x.Distinct < 1 || x.Path.Requests > x.Requests || x.Path.Bytes > x.Bytes))
            throw new InvalidDataException("Invalid abuse path evidence.");
        return rows.GroupBy(x => x.Prefix).ToDictionary(x => x.Key,
            x => new AbusePaths(x.First().Requests, x.First().Bytes, x.First().Distinct, x.Select(y => y.Path).ToArray()));
    }

    internal async Task<IReadOnlyList<AbuseImpact>> AbuseImpacts(DateTimeOffset through, IReadOnlyCollection<string> prefixes, CancellationToken ct)
    {
        if (prefixes.Count == 0) return [];
        var rows = await QueryAsync($$"""
            WITH {{AbusePrefixSql}} IN {prefixes:Array(String)} AS actor,
                 ts >= {five:DateTime} AS recent,
                 ts < {baselineEnd:DateTime} AS baseline
            SELECT lower(splitByChar(':', host)[1]), countIf(actor AND recent),
                   countIf(actor AND recent AND status >= 500 AND status < 600),
                   countIf(recent AND status >= 500 AND status < 600),
                   countIf(NOT actor AND recent), quantileExactIf(0.95)(duration_ms, NOT actor AND recent),
                   countIf(NOT actor AND baseline), quantileExactIf(0.95)(duration_ms, NOT actor AND baseline),
                   if(countIf(recent) = 0, 0., greatest(countIf(actor AND recent) / countIf(recent),
                       if(sumIf(bytes_sent, recent) = 0, 0., sumIf(bytes_sent, actor AND recent) / sumIf(bytes_sent, recent)))),
                   countIf(actor AND ts >= {two:DateTime}),
                   countIf(actor AND ts >= {two:DateTime} AND status >= 500 AND status < 600)
            FROM logs.caddy_requests
            WHERE ts >= {from:DateTime} AND ts < {through:DateTime} AND status != 101
              AND ({all:UInt8} = 1 OR lower(splitByChar(':', host)[1]) IN {hosts:Array(String)})
            GROUP BY 1
            HAVING countIf(actor AND recent) > 0
            SETTINGS readonly=1, max_threads=1, max_execution_time=30, max_memory_usage=600000000,
                     max_result_rows=10000, result_overflow_mode='throw'
            """, r => new AbuseImpact(r.GetString(0), Convert.ToUInt64(r.GetValue(1), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(2), CultureInfo.InvariantCulture), Convert.ToUInt64(r.GetValue(3), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(4), CultureInfo.InvariantCulture), Convert.ToDouble(r.GetValue(5), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(6), CultureInfo.InvariantCulture), Convert.ToDouble(r.GetValue(7), CultureInfo.InvariantCulture),
                Convert.ToDouble(r.GetValue(8), CultureInfo.InvariantCulture), Convert.ToUInt64(r.GetValue(9), CultureInfo.InvariantCulture),
                Convert.ToUInt64(r.GetValue(10), CultureInfo.InvariantCulture)), AbuseParameters(through, 30, prefixes)).WaitAsync(ct);
        return rows.Where(x => Uri.CheckHostName(x.Host) == UriHostNameType.Dns).ToArray();
    }
}
