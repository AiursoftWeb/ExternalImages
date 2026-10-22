namespace HttpErrorBridge;

internal readonly record struct Counts(ulong Requests, ulong Errors)
{
    internal double? Rate => Requests == 0 ? null : (double)Errors / Requests;
    public static Counts operator +(Counts a, Counts b) => new(checked(a.Requests + b.Requests), checked(a.Errors + b.Errors));
}

internal sealed record Baseline(double P95, double P99, int Samples, int Days, DateTimeOffset AsOf)
{
    internal double Sev3 => Math.Max(P95, 0.01);
    internal double Sev2 => Math.Max(P99, 0.05);
}

internal sealed record Observation(DateTimeOffset Through, Counts[] Minutes)
{
    internal Counts Window => Sum(15);
    internal Counts Latest => Minutes[^1];
    internal Counts Five => Sum(5);
    private Counts Sum(int length) => Minutes.TakeLast(length).Aggregate(new Counts(), (a, b) => a + b);

    internal bool Ordinary(Baseline? baseline) => baseline != null && Window.Requests >= 100 &&
        Window.Errors >= 10 && Window.Rate >= baseline.Sev3 && Latest.Errors > 0 && Latest.Rate >= 0.01;

    internal bool Severe(double? threshold) => threshold.HasValue && Window.Requests >= 100 &&
        Window.Errors >= 150 && Window.Rate >= threshold && Latest.Rate >= 0.05;

    internal bool Blackout => Minutes.TakeLast(3).All(x => x.Requests > 0 && x.Rate >= 0.90) &&
        Minutes.TakeLast(3).Aggregate(0UL, (a, b) => checked(a + b.Errors)) >= 5;

    internal bool Healthy(double threshold) => Window.Requests > 0 && Window.Rate < threshold / 2 &&
        Five.Requests > 0 && Five.Rate <= threshold / 2;
}

internal sealed class Sequence
{
    private long _last = -1;
    internal int Count { get; private set; }

    internal bool Step(long minute, bool matches, int required)
    {
        if (minute == _last) return Count >= required && matches;
        Count = matches ? (_last == minute - 1 ? Count + 1 : 1) : 0;
        _last = minute;
        return Count >= required;
    }
}

internal sealed class Evidence
{
    internal Guid? Incident { get; set; }
    internal Sequence Failure { get; } = new();
    internal Sequence Severe { get; } = new();
    internal Sequence Healthy { get; } = new();
}
