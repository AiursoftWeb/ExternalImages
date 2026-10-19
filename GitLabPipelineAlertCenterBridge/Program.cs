using Aiursoft.AlertCenter.SDK;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace PipelineBridge;

internal static class Program
{
    internal const string HealthFile = "/tmp/pipeline-bridge-health";

    public static async Task<int> Main(string[] args)
    {
        if (args is ["--healthcheck"])
            return File.Exists(HealthFile) && DateTime.UtcNow - File.GetLastWriteTimeUtc(HealthFile) < TimeSpan.FromMinutes(15) ? 0 : 1;
        var settings = Settings.Load();
        var builder = Host.CreateApplicationBuilder(args);
        builder.Logging.ClearProviders();
        builder.Logging.AddJsonConsole();
        builder.Services.AddSingleton(settings);
        builder.Services.AddSingleton<GitLabToken>();
        builder.Services.AddSingleton<GitLab>();
        builder.Services.AddAlertCenterSdk(settings.AlertCenterUrl, settings.AlertCenterKey);
        builder.Services.AddScoped<Reporter>();
        builder.Services.AddScoped<NinjaReporter>();
        builder.Services.AddHostedService<Scanner>();
        await builder.Build().RunAsync();
        return 0;
    }
}

internal sealed record Settings(string GitLabUrl, string GitLabToken, string AlertCenterUrl, string AlertCenterKey, string[] Groups, string TokenDirectory)
{
    internal static Settings Load()
    {
        static string Required(string name) => Environment.GetEnvironmentVariable(name) is { } value && !string.IsNullOrWhiteSpace(value)
            ? value.Trim() : throw new InvalidOperationException($"{name} is required.");
        static string Endpoint(string name)
        {
            var value = Required(name);
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https") ||
                uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0)
                throw new InvalidOperationException($"{name} must be an HTTP(S) endpoint without credentials, query or fragment.");
            return value.TrimEnd('/');
        }
        var groups = Required("GITLAB_GROUPS").Split(',').Select(x => x.Trim()).Where(x => x.Length > 0).Distinct(StringComparer.Ordinal).ToArray();
        if (groups.Length == 0 || groups.Any(x => x.StartsWith('/') || x.EndsWith('/') || x.Contains("//")))
            throw new InvalidOperationException("GITLAB_GROUPS must contain exact group paths.");
        return new Settings(Endpoint("GITLAB_URL"), Required("GITLAB_ACCESS_TOKEN"),
            Endpoint("ALERTCENTER_URL"), Required("ALERTCENTER_API_KEY"), groups,
            Environment.GetEnvironmentVariable("TOKEN_DIRECTORY") ?? "/data");
    }
}

internal sealed class Scanner(GitLab gitlab, GitLabToken token, IServiceScopeFactory scopes, ILogger<Scanner> log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            // Rotation failures must stop reporting rather than reuse a possibly revoked token.
            await token.Refresh(stoppingToken);
            var complete = true;
            try
            {
                // Refresh scope on every scan: removed permissions and moved projects must not use stale scope.
                var projects = await gitlab.Projects(stoppingToken);
                foreach (var project in projects)
                {
                    try
                    {
                        await using var scope = scopes.CreateAsyncScope();
                        await scope.ServiceProvider.GetRequiredService<NinjaReporter>().Reconcile(project, stoppingToken);
                    }
                    catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { throw; }
                    catch (Exception ex)
                    {
                        complete = false;
                        log.LogWarning("Project {ProjectId} NugetNinja scan failed ({ErrorType}); retry on next scan", project.Id, ex.GetType().Name);
                    }
                    try
                    {
                        var state = await gitlab.Read(project, stoppingToken);
                        if (state == null) continue;
                        await using var scope = scopes.CreateAsyncScope();
                        await scope.ServiceProvider.GetRequiredService<Reporter>().Reconcile(project, state, stoppingToken);
                    }
                    catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { throw; }
                    catch (Exception ex)
                    {
                        complete = false;
                        // Never log HTTP bodies, request headers or exceptions containing credentials.
                        log.LogWarning("Project {ProjectId} scan failed ({ErrorType}); retry on next scan", project.Id, ex.GetType().Name);
                    }
                }
                log.LogInformation("Scan completed: {ProjectCount} projects, healthy={Healthy}", projects.Count, complete);
                if (complete) await File.WriteAllTextAsync(Program.HealthFile, DateTimeOffset.UtcNow.ToString("O"), stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
            catch (Exception ex) { log.LogError("Discovery failed ({ErrorType}); no reporting this scan", ex.GetType().Name); }
            await Task.Delay(TimeSpan.FromMinutes(1), stoppingToken);
        }
    }
}
