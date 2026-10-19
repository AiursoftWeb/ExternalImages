using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace PipelineBridge;

internal sealed class GitLabToken(Settings settings, ILogger<GitLabToken> log) : IDisposable
{
    private sealed record StoredToken(string Endpoint, string Token, DateTimeOffset RotatedAt, bool RotationPending);
    private sealed record Rotation([property: JsonPropertyName("token")] string Token);
    private readonly HttpClient http = new(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(30) };
    private StoredToken? stored;
    private FileStream? lease;
    private string StatePath => Path.Combine(settings.TokenDirectory, "gitlab-token.json");
    internal string Current => stored?.Token ?? throw new InvalidOperationException("GitLab token has not been initialized.");

    internal async Task Refresh(CancellationToken ct)
    {
        if (stored == null)
        {
            Directory.CreateDirectory(settings.TokenDirectory);
            if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(settings.TokenDirectory, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            // A single writer owns the persistent token throughout the process lifetime.
            lease = new FileStream(Path.Combine(settings.TokenDirectory, "gitlab-token.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            if (File.Exists(StatePath))
            {
                await using var stream = File.OpenRead(StatePath);
                stored = await JsonSerializer.DeserializeAsync<StoredToken>(stream, cancellationToken: ct)
                    ?? throw new InvalidOperationException("The saved GitLab credential is invalid; restore it before restarting.");
                if (stored.Endpoint != settings.GitLabUrl || string.IsNullOrWhiteSpace(stored.Token))
                    throw new InvalidOperationException("The saved GitLab credential does not match this endpoint.");
            }
            else stored = new StoredToken(settings.GitLabUrl, settings.GitLabToken, DateTimeOffset.MinValue, false);
        }
        if (stored.RotationPending)
            throw new InvalidOperationException("GitLab rotation was interrupted. Install a fresh token and remove gitlab-token.json before restarting; do not retry the old token.");
        if (DateTimeOffset.UtcNow - stored.RotatedAt < TimeSpan.FromDays(1)) return;

        // Persist intent before the non-idempotent request. Never automatically retry a revoked token.
        await Save(stored with { RotationPending = true });
        using var request = new HttpRequestMessage(HttpMethod.Post, settings.GitLabUrl + "/api/v4/personal_access_tokens/self/rotate");
        request.Headers.Add("PRIVATE-TOKEN", stored.Token);
        using var response = await http.SendAsync(request, ct);
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException($"GitLab token rotation failed (HTTP {(int)response.StatusCode}); check the dedicated token's read_api/self_rotate scopes and saved rotation state.");
        var result = await response.Content.ReadFromJsonAsync<Rotation>(ct);
        if (string.IsNullOrWhiteSpace(result?.Token)) throw new InvalidOperationException("GitLab rotation returned no token; recover the credential before restarting.");
        var next = new StoredToken(settings.GitLabUrl, result.Token, DateTimeOffset.UtcNow, false);
        // Saving is deliberately independent of shutdown cancellation after GitLab has revoked the old token.
        await Save(next);
        stored = next;
        log.LogInformation("GitLab token rotated and saved; next rotation in 24 hours");
    }

    private async Task Save(StoredToken value)
    {
        var temporary = StatePath + ".tmp";
        var options = new FileStreamOptions { Mode = FileMode.Create, Access = FileAccess.Write, Share = FileShare.None };
        if (!OperatingSystem.IsWindows()) options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
        await using (var stream = new FileStream(temporary, options))
        {
            await JsonSerializer.SerializeAsync(stream, value);
            await stream.FlushAsync();
            stream.Flush(flushToDisk: true);
        }
        File.Move(temporary, StatePath, overwrite: true);
    }

    public void Dispose()
    {
        http.Dispose();
        lease?.Dispose();
    }
}
