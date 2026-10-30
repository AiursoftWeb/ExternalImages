using Newtonsoft.Json;
using Aiursoft.AlertCenter.SDK.Models;

namespace TrafficBridge;

internal sealed class StateStore(Settings settings)
{
    private readonly Dictionary<string, Evidence> _evidence = new(StringComparer.Ordinal);
    private Dictionary<string, CreateIncidentRequest>? _pending;
    private string PendingPath => Path.Combine(settings.DataDirectory, "pending.json");

    internal FileStream Acquire()
    {
        Directory.CreateDirectory(settings.DataDirectory);
        return new FileStream(Path.Combine(settings.DataDirectory, "lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
    }

    internal Evidence For(string key) => _evidence.TryGetValue(key, out var evidence)
        ? evidence : _evidence[key] = new Evidence();

    internal void Reset(string key) => _evidence.Remove(key);
    internal void BreakSequences() => _evidence.Clear();
    internal void BreakTrafficSequences()
    {
        foreach (var key in _evidence.Keys.Where(x => x != "collection").ToArray()) _evidence.Remove(key);
    }

    private Dictionary<string, CreateIncidentRequest> Pending => _pending ??= File.Exists(PendingPath)
        ? JsonConvert.DeserializeObject<Dictionary<string, CreateIncidentRequest>>(File.ReadAllText(PendingPath))
            ?? throw new InvalidDataException("Invalid pending state.")
        : new Dictionary<string, CreateIncidentRequest>(StringComparer.Ordinal);

    internal IEnumerable<string> PendingKeys => Pending.Keys;

    internal void Retain(HashSet<string> keys)
    {
        foreach (var key in _evidence.Keys.Where(x => !keys.Contains(x)).ToArray()) _evidence.Remove(key);
    }

    internal CreateIncidentRequest? Find(string key) => Pending.TryGetValue(key, out var id) ? id : null;

    internal CreateIncidentRequest Prepare(string key, CreateIncidentRequest report)
    {
        if (Find(key) is { } existing) return existing;
        Pending[key] = report;
        Save();
        return report;
    }

    internal void Forget(string key)
    {
        if (Pending.Remove(key)) Save();
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
            var bytes = System.Text.Encoding.UTF8.GetBytes(JsonConvert.SerializeObject(value));
            file.Write(bytes);
            file.Flush(flushToDisk: true);
        }
        File.Move(temp, path, overwrite: true);
    }
}
