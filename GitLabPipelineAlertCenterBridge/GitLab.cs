using System.Net;
using System.Net.Http.Json;
using System.Text.Json.Serialization;

namespace PipelineBridge;

internal sealed record Group(long Id, [property: JsonPropertyName("full_path")] string FullPath);
internal sealed record Project(long Id, string Name, [property: JsonPropertyName("default_branch")] string? DefaultBranch,
    Namespace Namespace, bool Archived, [property: JsonPropertyName("forked_from_project")] object? ForkedFromProject);
internal sealed record Namespace(string Kind, [property: JsonPropertyName("full_path")] string FullPath);
internal sealed record Pipeline(long Id, string Status, string Ref, string Source,
    [property: JsonPropertyName("web_url")] string WebUrl,
    [property: JsonPropertyName("finished_at")] DateTimeOffset? FinishedAt, bool Tag);
internal sealed record BranchState(Pipeline Latest, long SuccessBoundary, DateTimeOffset? FailedSince, long? FirstFailureId)
{
    internal string Fingerprint => $"{Latest.Id}:{Latest.Status}:{SuccessBoundary}:{FailedSince:O}:{FirstFailureId}";
}

internal sealed class GitLab : IDisposable
{
    private readonly HttpClient http;
    private readonly Settings settings;

    public GitLab(Settings settings)
    {
        this.settings = settings;
        http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
        {
            BaseAddress = new Uri(settings.GitLabUrl + "/api/v4/"), Timeout = TimeSpan.FromSeconds(30)
        };
        http.DefaultRequestHeaders.Add("PRIVATE-TOKEN", settings.GitLabToken);
    }

    private async Task<HttpResponseMessage> Get(string path, CancellationToken ct)
    {
        for (var attempt = 0; ; attempt++)
        {
            var response = await http.GetAsync(path, ct);
            if (response.StatusCode != HttpStatusCode.TooManyRequests || attempt >= 2)
            {
                if (!response.IsSuccessStatusCode)
                {
                    var status = response.StatusCode;
                    response.Dispose();
                    throw new HttpRequestException("GitLab request failed.", null, status);
                }
                return response;
            }
            var retry = response.Headers.RetryAfter;
            var delay = retry?.Delta ?? (retry?.Date - DateTimeOffset.UtcNow) ?? TimeSpan.FromSeconds(30);
            response.Dispose();
            // Do not retry earlier than GitLab requested. Cancellation interrupts long rate-limit waits.
            await Task.Delay(delay > TimeSpan.Zero ? delay : TimeSpan.FromSeconds(1), ct);
        }
    }

    private async Task<T> One<T>(string path, CancellationToken ct)
    {
        using var response = await Get(path, ct);
        return await response.Content.ReadFromJsonAsync<T>(ct) ?? throw new InvalidOperationException("Empty GitLab response.");
    }

    private async IAsyncEnumerable<T> Pages<T>(string path, [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
    {
        var page = 1;
        while (true)
        {
            using var response = await Get($"{path}{(path.Contains('?') ? '&' : '?')}per_page=100&page={page}", ct);
            var items = await response.Content.ReadFromJsonAsync<List<T>>(ct) ?? throw new InvalidOperationException("Empty GitLab page.");
            foreach (var item in items) yield return item;
            if (!response.Headers.TryGetValues("X-Next-Page", out var values))
            {
                if (items.Count == 0) yield break;
                page++;
                continue;
            }
            var next = values.Single();
            if (string.IsNullOrEmpty(next)) yield break;
            if (!int.TryParse(next, out var number) || number <= page) throw new InvalidOperationException("Invalid GitLab pagination.");
            page = number;
        }
    }

    internal async Task<List<Project>> Projects(CancellationToken ct)
    {
        var projects = new Dictionary<long, Project>();
        foreach (var path in settings.Groups)
        {
            var group = await One<Group>($"groups/{Uri.EscapeDataString(path)}", ct);
            if (group.FullPath != path) throw new InvalidOperationException("GitLab group did not match configured path.");
            await foreach (var project in Pages<Project>($"groups/{group.Id}/projects?include_subgroups=true&with_shared=false&archived=false", ct))
            {
                if (project.Archived || project.ForkedFromProject != null || string.IsNullOrEmpty(project.DefaultBranch) || project.Namespace.Kind != "group") continue;
                if (project.Namespace.FullPath != path && !project.Namespace.FullPath.StartsWith(path + "/", StringComparison.Ordinal)) continue;
                projects[project.Id] = project;
            }
        }
        return projects.Values.OrderBy(x => x.Id).ToList();
    }

    internal async Task<BranchState?> Read(Project project, CancellationToken ct)
    {
        Pipeline? latest = null;
        DateTimeOffset? failedSince = null;
        long? firstFailureId = null;
        long success = 0;
        await foreach (var entry in Pages<Pipeline>($"projects/{project.Id}/pipelines?ref={Uri.EscapeDataString(project.DefaultBranch!)}&order_by=id&sort=desc", ct))
        {
            if (entry.Source is "merge_request_event" or "parent_pipeline" || entry.Status is not ("success" or "failed")) continue;
            var pipeline = await One<Pipeline>($"projects/{project.Id}/pipelines/{entry.Id}", ct);
            if (pipeline.Tag || pipeline.Ref != project.DefaultBranch || pipeline.Source is "merge_request_event" or "parent_pipeline") continue;
            // A retry can change status between the list and detail request. Recompute next scan.
            if (pipeline.Status != entry.Status) throw new InvalidOperationException("Pipeline changed during scan.");
            latest ??= pipeline;
            if (pipeline.Status == "success") { success = pipeline.Id; break; }
            if (pipeline.FinishedAt is not { } finished || finished > DateTimeOffset.UtcNow)
                throw new InvalidOperationException("Failed pipeline has no valid completion time.");
            if (failedSince == null || finished < failedSince) { failedSince = finished; firstFailureId = pipeline.Id; }
        }
        return latest == null ? null : new BranchState(latest, success, failedSince, firstFailureId);
    }

    public void Dispose() => http.Dispose();
}
