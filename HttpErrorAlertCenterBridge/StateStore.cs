using System.Text.Json;

namespace HttpErrorBridge;

internal sealed class StateStore(Settings settings)
{
    private readonly Dictionary<string, Evidence> _evidence = new(StringComparer.Ordinal);
    private Dictionary<string, Guid>? _pending;
    private string PendingPath => Path.Combine(settings.DataDirectory, "pending.json");

    internal FileStream Acquire()
    {
        Directory.CreateDirectory(settings.DataDirectory);
        return new FileStream(Path.Combine(settings.DataDirectory, "lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
    }

    internal Evidence For(string host) => _evidence.TryGetValue(host, out var evidence)
        ? evidence : _evidence[host] = new Evidence();

    internal void Reset(string host) => _evidence.Remove(host);
    internal void BreakSequences() => _evidence.Clear();

    private Dictionary<string, Guid> Pending => _pending ??= File.Exists(PendingPath)
        ? JsonSerializer.Deserialize<Dictionary<string, Guid>>(File.ReadAllBytes(PendingPath))
            ?? throw new InvalidDataException("Invalid pending state.")
        : new Dictionary<string, Guid>(StringComparer.Ordinal);

    internal IEnumerable<string> PendingHosts => Pending.Keys;

    internal void Retain(HashSet<string> hosts)
    {
        foreach (var host in _evidence.Keys.Where(x => !hosts.Contains(x)).ToArray()) _evidence.Remove(host);
    }

    internal Guid? Find(string host) => Pending.TryGetValue(host, out var id) ? id : null;

    internal Guid Prepare(string host)
    {
        if (Find(host) is { } existing) return existing;
        var id = Guid.NewGuid();
        Pending[host] = id;
        Save();
        return id;
    }

    internal void Forget(string host)
    {
        if (Pending.Remove(host)) Save();
    }

    private void Save()
    {
        try { AtomicWrite(PendingPath, Pending); }
        catch { _pending = null; throw; }
    }

    internal static void AtomicWrite<T>(string path, T value)
    {
        var temp = path + ".tmp";
        using (var file = new FileStream(temp, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(file, value);
            file.Flush(flushToDisk: true);
        }
        File.Move(temp, path, overwrite: true);
    }
}
