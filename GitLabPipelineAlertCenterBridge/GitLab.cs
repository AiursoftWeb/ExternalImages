using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace PipelineBridge;

internal sealed record Group(long Id, [property: JsonPropertyName("full_path")] string FullPath);
internal sealed record Project(long Id, string Name, [property: JsonPropertyName("default_branch")] string? DefaultBranch,
    Namespace Namespace, bool Archived, [property: JsonPropertyName("forked_from_project")] object? ForkedFromProject);
internal sealed record ProjectSettings(
    [property: JsonPropertyName("builds_access_level")] string? BuildsAccessLevel,
    [property: JsonPropertyName("ci_config_path")] string? CiConfigPath,
    [property: JsonPropertyName("auto_devops_enabled")] bool? AutoDevOpsEnabled,
    [property: JsonPropertyName("path_with_namespace")] string? PathWithNamespace);
internal sealed record RepositoryBranch(RepositoryCommit Commit);
internal sealed record RepositoryCommit(string Id);
internal sealed record Namespace(string Kind, [property: JsonPropertyName("full_path")] string FullPath);
internal sealed record Pipeline(long Id, string Status, string Ref, string Source,
    [property: JsonPropertyName("web_url")] string WebUrl,
    [property: JsonPropertyName("finished_at")] DateTimeOffset? FinishedAt, bool Tag);
internal sealed record BranchState(Pipeline Latest, long SuccessBoundary, DateTimeOffset? FailedSince, long? FirstFailureId)
{
    internal string Fingerprint => $"{Latest.Id}:{Latest.Status}:{SuccessBoundary}:{FailedSince:O}:{FirstFailureId}";
}

internal sealed record MergeRequestAuthor(string Username);
internal sealed record MergeRequestPipeline(long Id, string Status, string Sha,
    [property: JsonPropertyName("web_url")] string WebUrl);
internal sealed record MergeRequest(long Iid, string Title, string? Description, string State, string Sha, MergeRequestAuthor Author,
    [property: JsonPropertyName("target_project_id")] long TargetProjectId,
    [property: JsonPropertyName("created_at")] DateTimeOffset CreatedAt,
    [property: JsonPropertyName("merged_at")] DateTimeOffset? MergedAt,
    [property: JsonPropertyName("web_url")] string WebUrl,
    [property: JsonPropertyName("detailed_merge_status")] string? DetailedMergeStatus,
    [property: JsonPropertyName("has_conflicts")] bool HasConflicts, bool Draft,
    [property: JsonPropertyName("head_pipeline")] MergeRequestPipeline? HeadPipeline);
internal sealed record MergeRequestFile([property: JsonPropertyName("new_path")] string NewPath);
internal sealed record PipelineJob(long Id, string Name, string Status,
    [property: JsonPropertyName("allow_failure")] bool AllowFailure,
    [property: JsonPropertyName("failure_reason")] string? FailureReason,
    [property: JsonPropertyName("web_url")] string WebUrl);

internal sealed class GitLab : IDisposable
{
    private readonly HttpClient http;
    private readonly Settings settings;
    private readonly GitLabToken token;

    public GitLab(Settings settings, GitLabToken token)
    {
        this.settings = settings;
        this.token = token;
        http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
        {
            BaseAddress = new Uri(settings.GitLabUrl + "/api/v4/"), Timeout = TimeSpan.FromSeconds(30)
        };
    }

    private async Task<HttpResponseMessage> Get(string path, CancellationToken ct)
    {
        for (var attempt = 0; ; attempt++)
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, path);
            request.Headers.Add("PRIVATE-TOKEN", token.Current);
            var response = await http.SendAsync(request, ct);
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

    internal async Task<List<MergeRequest>> NinjaMergeRequests(Project project, CancellationToken ct)
    {
        var result = new List<MergeRequest>();
        await foreach (var mr in Pages<MergeRequest>($"projects/{project.Id}/merge_requests?state=opened&scope=all&author_username=nuget-ninja", ct))
        {
            if (mr.Author.Username == "nuget-ninja" && mr.TargetProjectId == project.Id) result.Add(mr);
        }
        return result;
    }

    internal async Task<MergeRequest?> LatestNinjaMerge(Project project, CancellationToken ct)
    {
        var requests = await One<List<MergeRequest>>($"projects/{project.Id}/merge_requests?state=merged&scope=all&author_username=nuget-ninja&order_by=merged_at&sort=desc&per_page=1", ct);
        var latest = requests.SingleOrDefault();
        if (latest != null && (latest.Author.Username != "nuget-ninja" || latest.TargetProjectId != project.Id ||
            latest.Iid <= 0 || latest.CreatedAt == default || latest.State != "merged" || latest.MergedAt == null ||
            latest.MergedAt < latest.CreatedAt || latest.MergedAt > DateTimeOffset.UtcNow || string.IsNullOrWhiteSpace(latest.WebUrl)))
            throw new InvalidOperationException("Invalid NugetNinja merge history.");
        return latest;
    }

    internal Task<MergeRequest> ReadMergeRequest(Project project, long iid, CancellationToken ct) =>
        One<MergeRequest>($"projects/{project.Id}/merge_requests/{iid}", ct);

    internal async Task<List<string>> ChangedFiles(Project project, long iid, CancellationToken ct)
    {
        var result = new List<string>();
        await foreach (var file in Pages<MergeRequestFile>($"projects/{project.Id}/merge_requests/{iid}/diffs", ct))
        {
            result.Add(file.NewPath);
            if (result.Count == 10) break;
        }
        return result;
    }

    internal async Task<List<PipelineJob>> FailedJobs(Project project, long pipelineId, CancellationToken ct)
    {
        var result = new List<PipelineJob>();
        await foreach (var job in Pages<PipelineJob>($"projects/{project.Id}/pipelines/{pipelineId}/jobs?scope[]=failed&include_retried=false", ct))
            if (job.Status == "failed" && !job.AllowFailure) result.Add(job);
        return result;
    }

    internal async Task<BranchState?> Read(Project project, CancellationToken ct)
    {
        if (!await HasCiConfiguration(project, ct)) return null;
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

    private async Task<bool> HasCiConfiguration(Project project, CancellationToken ct)
    {
        var details = await One<ProjectSettings>($"projects/{project.Id}", ct);
        if (details.BuildsAccessLevel == "disabled") return false;
        if (details.AutoDevOpsEnabled == true) return true;
        // Explicit configuration can live in another project or at an external URL.
        // Its absence here does not establish retirement; retain normal monitoring.
        if (!string.IsNullOrWhiteSpace(details.CiConfigPath)) return true;
        // Resolve an accessible branch first: a missing project/ref is not a missing file.
        var branch = await One<RepositoryBranch>($"projects/{project.Id}/repository/branches/{Uri.EscapeDataString(project.DefaultBranch!)}", ct);
        try
        {
            using var response = await Get($"projects/{project.Id}/repository/files/.gitlab-ci.yml?ref={Uri.EscapeDataString(branch.Commit.Id)}", ct);
            return true;
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            // Reporter tokens cannot always see auto_devops_enabled. Verify the actual
            // pipeline configuration source before treating a missing local file as retirement.
            await foreach (var pipeline in Pages<Pipeline>($"projects/{project.Id}/pipelines?ref={Uri.EscapeDataString(project.DefaultBranch!)}&order_by=id&sort=desc", ct))
            {
                if (pipeline.Source is "merge_request_event" or "parent_pipeline") continue;
                if (string.IsNullOrWhiteSpace(details.PathWithNamespace))
                    throw new InvalidOperationException("Missing project path for configuration verification.");
                var query = "{ project(fullPath: " + JsonSerializer.Serialize(details.PathWithNamespace) +
                    ") { pipeline(id: \"gid://gitlab/Ci::Pipeline/" + pipeline.Id + "\") { configSource } } }";
                var result = await One<JsonElement>("../graphql?query=" + Uri.EscapeDataString(query), ct);
                if (result.TryGetProperty("errors", out _)) throw new InvalidOperationException("Cannot verify CI configuration source.");
                var source = result.GetProperty("data").GetProperty("project").GetProperty("pipeline").GetProperty("configSource").GetString();
                if (string.IsNullOrEmpty(source)) throw new InvalidOperationException("Missing CI configuration source.");
                return source != "REPOSITORY_SOURCE";
            }
            return false;
        }
    }

    public void Dispose() => http.Dispose();
}
