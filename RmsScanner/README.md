# CPU chart scanner

HTTP/CLI compatibility adapter around the production `curve-chart-beauty` image pipeline.
The core HTTP interface remains compatible; host setup,
processing semantics, diagnostics and stage input acceptance are not identical to
those of the former scanner.
All processing is local: **EXIF orientation/RGB PNG preprocessing, max side 2048 →
MBCNN demoire (CPU, no extra resizing) → chart detection, perspective correction,
sharpening and white background**. Chart processing is always enabled. Detection or
model failure is an error, never a successful original-image/legacy fallback.
This changes cropping and appearance compared with the former full-frame scanner.
No OCR, reconstruction or guarantee of recovering invisible/clipped labels.

## Build and start

The Linux amd64 image runs the three processing stages on the CPU. Put an
authorized checkpoint in a file readable by UID 10001. For the RMS production
checkpoint, verify its digest against [SOURCE-NOTICES.md](SOURCE-NOTICES.md).

```sh
docker build -t rms-scanner .
docker run --rm --name scanner --network workshop \
  --mount type=bind,src="$(realpath default.h5)",dst=/app/models/default.h5,readonly \
  --mount type=volume,src=scanner-data,dst=/data \
  rms-scanner
```

Use the same private Docker network as RMS (named `workshop` in the example).
RMS sends multipart uploads to `http://scanner:9863`. Only callers using
`/process` with a file path need a shared input directory. Persist `/data` for
result downloads across restarts. The HTTP service has no authentication.
Allow the proxy enough time for CPU inference, which can exceed one minute on
a 2048px photo. Run one uvicorn worker per container to preserve the capacity
limit.

## HTTP contract

- `GET /health` → `{"status":"ok"}`. Lightweight liveness only; model is checked
  during processing. An unreadable or empty model file prevents the container from starting. Inference failures log details and return 500.
- `POST /process`: JSON `{"input_path":"plot.png"}` plus optional legacy options.
  Relative paths are rooted at `INPUT_DIR`; absolute paths must remain inside it.
- `POST /process/upload`: multipart file field **image**, optional
  `response_type=json` (default) or `image`, and optional legacy options.
- `GET /results/{request_id}/image`: persistent PNG download.

Both JSON processing routes retain outer fields `request_id`, `input_path`,
`output_path`, `download_url`, `diagnostic` (object). Upload `input_path` is null.
PNG response uses `image/png`, attachment filename `image.png`, `X-Request-ID`.
Each successful HTTP job retains **only `stem.png` and `.result.json`** under
`OUTPUT_DIR/<request_id>/`. The index contains only `output_path`; downloads work
after restart and still accept older full-response indexes. The full diagnostic
object (fixed parameters and three stage records, not old algorithm metrics)
remains in the immediate JSON response **in memory only**: no `stem.json`, persistent
diagnostic, manifest or intermediate image is written. CLI `scan.py` retains only
the final PNG. Pipeline protocol files and intermediate images are temporary and
removed on success, failure or cancellation after CPU subprocesses finish. Failed
or cancelled HTTP jobs leave no result. Existing successful results are not
removed; arrange your own retention for successful jobs. Operational `data/` is
never cleaned wholesale.

Parent-process structured JSON events go to stderr, not files or stage protocol
stdout. Normal logs focus on each actual `preprocess` / `demoire` / `chart-enhance`
runner begin/success/failure and one final job result. Redundant receipt,
validation, processing, output-publication and successful-cleanup events are
suppressed; preflight/cleanup failures remain visible. Fields are only `event`,
`requestId`, `stage` when applicable, `status`, elapsed milliseconds on completion,
and a bounded failure `reason`. The final job also includes the input name.
Sizes, dimensions, output paths and null placeholders are omitted.
CLI generates a per-image ID. Images and credentials
are never logged. Stage subprocess JSON stdout remains an internal protocol;
these events are emitted live by the parent, not replayed from manifests.
The lower-level `pipeline.py` protocol/debug entry point still produces a batch
manifest when explicitly used; production HTTP and `scan.py` wrap it in scratch.

```sh
curl http://scanner:9863/health
curl -H 'Content-Type: application/json' -d '{"input_path":"plot.png"}' http://scanner:9863/process
curl -F image=@photo.png http://scanner:9863/process/upload
curl -F image=@photo.png -F response_type=image http://scanner:9863/process/upload -o enhanced.png
```

### Legacy options are validated but **ignored**

| Option | Accepted range/default |
|---|---|
| `strength` | finite >0 and ≤3; default 1 |
| `max_side` | integer 128–4096; default 2400 |
| `no_demoire` | boolean; default false |
| `passes` | integer 1–2; default 2 |

These do not change the fixed CPU/2048/three-stage processing, even
`no_demoire=true`. Unknown/duplicate fields and invalid values return 422.
`scan.process(path, outdir, args)` is retained. CLI accepts a file or directory:
`python scan.py photo.png -o output`; old `--strength`, `--max-side`,
`--no-demoire`, `--passes`, `--no-crop`, `--corners`, `--debug` are compatibility
options, not alternate algorithms. CLI validation retains the original ranges:
`--strength` must be finite and positive (no upper bound), `--max-side` must be
at least 128 (no upper bound), and `--passes` is 1 or 2. Unlike HTTP, CLI accepts
strength above 3 and max-side above 4096, but still processes at fixed 2048.
`--threads` controls CLI stage CPU threads (default 4; HTTP environment default 2).
Before processing any image, CLI rejects duplicate stems in a batch and any PNG
or JSON output path overlapping an input, including resolved symlink aliases.
Use a separate output directory; originals must not be overwritten.

## Limits, storage and environment

Existing environment names/defaults remain: `INPUT_DIR=/data/input`,
`OUTPUT_DIR=/data/output`, `PROCESSING_SLOTS=1`, `OPENCV_THREADS=2`,
`MAX_FILE_BYTES=52428800`, `MAX_PIXELS=25000000`. Positive limits required.
CPU stages honor `OPENCV_THREADS`; Docker additionally bounds BLAS/OMP/MKL threads.
`MODEL_WEIGHTS` overrides the default mount path `/app/models/default.h5`. No weight download or training occurs.

Over file/body/pixel limits → 413; invalid parameters/images/unsafe paths → 422;
missing input/result → 404; saturated service → 503 with `Retry-After: 1`.
Only regular files inside INPUT_DIR can be opened, using no-follow directory
handles. UUID result directories isolate requests; uploads are bounded before
parsing and held in memory. Capacity is held until background CPU work AND stage
subprocesses finish, even if the client disconnects/cancels. Graceful shutdown
should allow in-flight CPU jobs to complete. Forced container termination cannot
promise cleanup; orphan hidden scratch may be removed only when no job is active.
Supported inputs: PNG/JPEG/TIFF/WebP/BMP, subject also to production stage checks
(no animated/multipage/high-bit-depth input; preprocessing hard ceiling 100MiB /
40M pixels). Use 8-bit single-frame images. Model unavailable is never liveness
failure or silent success.

## Provenance and authorization

The service is migrated from the RMS CPU chart scanner. The production model
is provided at deployment. See [SOURCE-NOTICES.md](SOURCE-NOTICES.md) for its
origin and checksum.

## Tests

```sh
docker build --platform linux/amd64 --target test .
```

The test stage runs the original HTTP, CLI, stage and storage regression tests.
A multipart upload additionally runs all three offline subprocesses and verifies
the returned PNG and persisted download using a generated, untrained identity
checkpoint. To validate image quality at a site, repeat the upload with that
site's checkpoint.
