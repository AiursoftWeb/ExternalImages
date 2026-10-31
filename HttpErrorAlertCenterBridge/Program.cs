using Aiursoft.AlertCenter.SDK;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HttpErrorBridge;

internal static class Program
{
    internal const string HealthFile = "/tmp/http-error-bridge-health";

    public static async Task<int> Main(string[] args)
    {
        if (args is ["--healthcheck"])
            return Fresh(HealthFile) && (!Settings.AbuseIsEnabled || Fresh(AbuseScanner.HealthFile)) ? 0 : 1;
        var settings = Settings.Load();
        var builder = Host.CreateApplicationBuilder(args);
        builder.Logging.ClearProviders();
        builder.Logging.AddJsonConsole();
        builder.Services.AddSingleton(settings);
        builder.Services.AddSingleton(TimeProvider.System);
        builder.Services.AddSingleton<StateStore>();
        builder.Services.AddSingleton<History>();
        builder.Services.AddScoped<RequestLog>();
        builder.Services.AddScoped<Reporter>();
        builder.Services.AddAlertCenterSdk(settings.AlertCenterUrl, settings.AlertCenterKey);
        builder.Services.AddHostedService<Scanner>();
        if (settings.AbuseEnabled)
        {
            builder.Services.AddSingleton<AbuseStore>();
            builder.Services.AddScoped<AbuseReporter>();
            builder.Services.AddHostedService<AbuseScanner>();
        }
        using var host = builder.Build();
        // Both scanners share the same process lease before either can perform API writes.
        using var lease = host.Services.GetRequiredService<StateStore>().Acquire();
        await host.RunAsync();
        return 0;
    }

    private static bool Fresh(string path) => File.Exists(path) && DateTime.UtcNow >= File.GetLastWriteTimeUtc(path) &&
        DateTime.UtcNow - File.GetLastWriteTimeUtc(path) < TimeSpan.FromMinutes(3);
}

internal sealed record Settings(string Server, string[] Hosts, string ConnectionString, string AlertCenterUrl, string AlertCenterKey, string DataDirectory)
{
    internal bool AllHosts => Hosts is ["*"];
    internal bool AbuseEnabled { get; init; }
    internal System.Net.IPNetwork[] AbuseExcludedSources { get; init; } = [];
    internal string AbuseSource => $"caddy-abuse:v1:{Server}";
    internal static bool AbuseIsEnabled => Environment.GetEnvironmentVariable("HTTP_ABUSE_ENABLED") is not { Length: > 0 } value
        ? false : bool.TryParse(value, out var enabled) ? enabled : throw new InvalidOperationException("Invalid HTTP_ABUSE_ENABLED.");
    internal string Source => $"http-error-bridge:v1:{Server}";

    internal static Settings Load()
    {
        static string Required(string name) => Environment.GetEnvironmentVariable(name) is { } value && !string.IsNullOrWhiteSpace(value)
            ? value.Trim() : throw new InvalidOperationException($"{name} is required.");
        var server = Required("SERVER_NAME");
        if (server.Length > 100 || server.Any(c => !char.IsAsciiLetterOrDigit(c) && c is not '-' and not '_' and not '.'))
            throw new InvalidOperationException("SERVER_NAME must be a stable server identifier.");
        var hosts = Required("MONITORED_HOSTS").Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)
            .Select(x => x.ToLowerInvariant()).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        if (!(hosts is ["*"]) && (hosts.Length is 0 or > 500 || hosts.Any(x => Uri.CheckHostName(x) != UriHostNameType.Dns)))
            throw new InvalidOperationException("MONITORED_HOSTS must be * or contain 1 to 500 exact DNS hostnames.");
        var endpoint = Required("ALERTCENTER_URL");
        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https") ||
            uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0)
            throw new InvalidOperationException("ALERTCENTER_URL must be an HTTP(S) endpoint without credentials, query or fragment.");
        return new Settings(server, hosts, Required("CLICKHOUSE_CONNECTION_STRING"), endpoint.TrimEnd('/'),
            Required("ALERTCENTER_API_KEY"), Environment.GetEnvironmentVariable("DATA_DIRECTORY") ?? "/data")
        {
            AbuseEnabled = AbuseIsEnabled,
            AbuseExcludedSources = (Environment.GetEnvironmentVariable("HTTP_ABUSE_EXCLUDED_SOURCE_CIDRS") ?? "")
                .Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)
                .Select(x => System.Net.IPNetwork.TryParse(x, out var network) &&
                    (network.BaseAddress.AddressFamily != System.Net.Sockets.AddressFamily.InterNetworkV6 || network.PrefixLength <= 64)
                    ? network : throw new InvalidOperationException("Invalid HTTP_ABUSE_EXCLUDED_SOURCE_CIDRS.")).ToArray()
        };
    }
}

internal sealed class Scanner(IServiceScopeFactory scopes, StateStore store, History history, Settings settings, ILogger<Scanner> log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            var now = DateTimeOffset.UtcNow;
            var minute = now.ToUnixTimeSeconds() / 60;
            var cutoff = DateTimeOffset.FromUnixTimeSeconds((minute - 1) * 60);
            try
            {
                await using var scope = scopes.CreateAsyncScope();
                var db = scope.ServiceProvider.GetRequiredService<RequestLog>();
                var recent = await db.Recent(cutoff, stoppingToken);
                // Recent source traffic is required even when all monitored hosts are quiet.
                if (recent.Latest < cutoff)
                    throw new InvalidOperationException("Request log collection is stale.");
                await history.Refresh(db, DateTimeOffset.FromUnixTimeSeconds(minute * 60), stoppingToken);
                var reporter = scope.ServiceProvider.GetRequiredService<Reporter>();
                var count = await reporter.Scan(recent, history, minute, cutoff, stoppingToken);
                await File.WriteAllTextAsync(Program.HealthFile, now.ToString("O"), stoppingToken);
                log.LogInformation("Scan completed for {Count} hosts on {Server}", count, settings.Server);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                // A missing minute breaks consecutive evidence; elapsed incident age remains authoritative.
                store.BreakSequences();
                log.LogWarning("Scan failed ({ErrorType}); no healthy evidence retained", ex.GetType().Name);
            }
            var next = DateTimeOffset.FromUnixTimeSeconds((DateTimeOffset.UtcNow.ToUnixTimeSeconds() / 60 + 1) * 60);
            await Task.Delay(next - DateTimeOffset.UtcNow > TimeSpan.Zero ? next - DateTimeOffset.UtcNow : TimeSpan.Zero, stoppingToken);
        }
    }
}
