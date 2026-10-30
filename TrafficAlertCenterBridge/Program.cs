using Aiursoft.AlertCenter.SDK;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace TrafficBridge;

internal static class Program
{
    internal const string HealthFile = "/tmp/traffic-bridge-health";

    public static async Task<int> Main(string[] args)
    {
        if (args is ["--healthcheck"])
            return File.Exists(HealthFile) && DateTime.UtcNow - File.GetLastWriteTimeUtc(HealthFile) < TimeSpan.FromMinutes(3) ? 0 : 1;
        var settings = Settings.Load();
        var builder = Host.CreateApplicationBuilder(args);
        builder.Logging.ClearProviders();
        builder.Logging.AddJsonConsole();
        builder.Services.AddSingleton(settings);
        builder.Services.AddSingleton(TimeProvider.System);
        builder.Services.AddSingleton<StateStore>();
        builder.Services.AddScoped<RequestLog>();
        builder.Services.AddScoped<Reporter>();
        builder.Services.AddAlertCenterSdk(settings.AlertCenterUrl, settings.AlertCenterKey);
        builder.Services.AddHostedService<Scanner>();
        await builder.Build().RunAsync();
        return 0;
    }
}

internal sealed record Settings(string Server, string[] Hosts, string ConnectionString, string AlertCenterUrl, string AlertCenterKey, string DataDirectory, System.Net.IPNetwork[] ExcludedSources)
{
    internal bool AllHosts => Hosts is ["*"];
    internal string Source => $"traffic-bridge:v1:{Server}";

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
            Required("ALERTCENTER_API_KEY"), Environment.GetEnvironmentVariable("DATA_DIRECTORY") ?? "/data",
            (Environment.GetEnvironmentVariable("EXCLUDED_SOURCE_CIDRS") ?? "").Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)
                .Select(x => System.Net.IPNetwork.TryParse(x, out var network) &&
                    (network.BaseAddress.AddressFamily != System.Net.Sockets.AddressFamily.InterNetworkV6 || network.PrefixLength <= 64) ? network : throw new InvalidOperationException("Invalid EXCLUDED_SOURCE_CIDRS.")).ToArray());
    }
}

internal sealed class Scanner(IServiceScopeFactory scopes, StateStore store, TimeProvider clock, ILogger<Scanner> log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        using var lease = store.Acquire();
        while (!ct.IsCancellationRequested)
        {
            var minute = clock.GetUtcNow().ToUnixTimeSeconds() / 60;
            var cutoff = DateTimeOffset.FromUnixTimeSeconds((minute - 1) * 60);
            try
            {
                await using var scope = scopes.CreateAsyncScope();
                Observation? observation = null;
                string? failure = null;
                try { observation = await scope.ServiceProvider.GetRequiredService<RequestLog>().Read(cutoff, ct); }
                catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
                catch (Exception ex) { failure = ex.GetType().Name; store.BreakTrafficSequences(); }
                await scope.ServiceProvider.GetRequiredService<Reporter>().Scan(observation, failure, minute, ct);
                if (observation != null && clock.GetUtcNow() - observation.Through <= TimeSpan.FromMinutes(3))
                    await File.WriteAllTextAsync(Program.HealthFile, clock.GetUtcNow().ToString("O"), ct);
                else log.LogWarning("Request log collection unavailable ({ErrorType})", failure);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception ex)
            {
                store.BreakSequences();
                log.LogWarning("Scan failed ({ErrorType}); no recovery evidence retained", ex.GetType().Name);
            }
            var next = DateTimeOffset.FromUnixTimeSeconds((clock.GetUtcNow().ToUnixTimeSeconds() / 60 + 1) * 60);
            await Task.Delay(next - clock.GetUtcNow() > TimeSpan.Zero ? next - clock.GetUtcNow() : TimeSpan.Zero, clock, ct);
        }
    }
}
