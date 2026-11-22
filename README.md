# Aiursoft ExternalImages

## Overview

### What is ExternalImages?

ExternalImages contains the Docker build contexts for Aiursoft's externally published service images. The release pipeline publishes matching tags to Docker Hub and the internal home registry. The collection includes gateways, speech/document services, monitoring collectors and alert bridges.

Each directory is an independent build context, not a service in one shared application. Choose the image, CPU architecture, configuration and persistent mounts required for that workload. The source Dockerfiles and existing component documents define its behavior.

## Installation

Install Docker for the intended Linux host and ensure it can reach the selected registry. GPU images additionally require a compatible NVIDIA driver and container runtime on that host. Registry access does not remove the image's runtime hardware or external-service requirements.

Pull a selected image:

```bash
docker pull docker.io/aiursoft/markitdown:latest
```

For internal consumers, the corresponding image is `hub.aiursoft.com/aiursoft/markitdown:latest`; other published images follow the same `aiursoft/<image>:<tag>` layout at both registries.

The catalog below describes the configured release jobs, not a live registry availability check. `latest` is mutable; inspect the pulled digest when recording a deployment. Availability for an architecture is limited to that image's configured platforms.

## Usage

### Image catalog

The release jobs in [`.gitlab-ci.yml`](.gitlab-ci.yml) define these image names and build platforms. Image links open the corresponding build-context Dockerfile; all platforms below are Linux:

| Image | CI platforms | Purpose |
| --- | --- | --- |
| [`gateway`](Gateway/Dockerfile) | amd64, arm64 | Xray gateway with runtime client UUID configuration. |
| [`asr-api`](AsrApi/Dockerfile) | amd64, arm64 | Go speech API and embedded web frontend. |
| [`folderagent`](FolderAgent/Dockerfile) | amd64, arm64 | Folder-scoped AI agent service; persistent workspace and provider authentication. |
| [`markitdown`](MarkItDown/Dockerfile) | amd64, arm64 | HTTP file-to-Markdown conversion service. |
| [`rms-scanner`](RmsScanner/README.md) | amd64 | Offline CPU enhancement of RMS curve photos. Mount the site checkpoint read-only and persist `/data`. |
| [`whisperx`](WhisperX/Dockerfile) | amd64 | CUDA transcription service with models baked into the image. |
| [`cloudflare_analytics_collector`](CloudflareAnalyticsCollector/Dockerfile) | amd64, arm64 | Cloudflare analytics collection into ClickHouse. |
| [`clickhouse_app_log_error_monitor`](ClickHouseAppLogErrorMonitor/Dockerfile) | amd64, arm64 | ClickHouse application-error monitoring and alert delivery. |
| [`shepherd-140fixed`](Shepherd140Fixed/Dockerfile) | amd64, arm64 | Shepherd image with the tracked #140 patch applied. |

### Run an image

For example, run MarkItDown bound to localhost, using the environment file from Configuration:

```bash
docker run --rm --name markitdown \
  -p 127.0.0.1:8000:8000 \
  --env-file markitdown.env \
  docker.io/aiursoft/markitdown:latest
```

In another terminal, inspect the health response and open the web interface:

```bash
curl --fail http://127.0.0.1:8000/health
```

Open `http://127.0.0.1:8000/`, provide the configured access token when prompted, and submit a supported document. `/health` reports `ok` or `degraded`; check the body as well as the HTTP status. A successful conversion returns the generated Markdown. Other images have different ports, credentials, permissions and storage requirements; use their component documentation rather than this example unchanged.

For a long-running container, inspect its logs and health state as well as the application-specific result. Preserve persistent mounts when replacing a container. Publishing a new image does not prove that a consuming Box has pulled, deployed or passed health checks with that image.

## Configuration

For the MarkItDown example, create a private `markitdown.env` file:

```dotenv
MARKDOWN_ACCESS_TOKEN=replace-with-your-token
MAX_FILE_SIZE_MB=50
```

`MARKDOWN_ACCESS_TOKEN` is optional in the implementation: leaving it empty disables bearer-token checks for `/api/` endpoints. `MAX_FILE_SIZE_MB` defaults to `50` and limits uploaded file size. Keep the token file out of source control. The web interface and `/health` remain accessible independently of API bearer checks.

Other image-specific inputs are separate contracts:

| Image | Configuration and persistent state |
| --- | --- |
| `gateway` | `XRAY_UUID` is required by [entry.sh](Gateway/entry.sh); the image uses the tracked [Xray configuration](Gateway/config.json) and `/var/log/xray`. |
| `asr-api` | Backend connections, credentials and HTTP settings are defined by [the server configuration](AsrApi/internal/server). |
| `folderagent` | Provider credentials, `/import` workspace and `/home/appuser/.codex` login state; see [FolderAgent](FolderAgent/README.md). |
| `whisperx` | Required `ASR_WHISPERX_TOKEN`, model/device/compute settings and optional `WHISPERX_PRELOAD`; see [WhisperX](WhisperX/README.md), [runtime](WhisperX/app.py) and [model choices](WhisperX/models_config.py). |
| `cloudflare_analytics_collector` | Cloudflare credentials, ClickHouse connection and polling interval; see [collector configuration](CloudflareAnalyticsCollector/README.md). |
| `clickhouse_app_log_error_monitor` | ClickHouse access, alert delivery and writable state directory; see [monitor configuration](ClickHouseAppLogErrorMonitor/README.md). |
| `shepherd-140fixed` | Inherits Shepherd's configuration; the [component guide](Shepherd140Fixed/README.md) explains the patch and required Docker/Swarm access. |

WhisperX build arguments are `SINGLE_MODEL=true` and `USE_PYPI_MIRROR=false` by Dockerfile default; its release job explicitly sets both to `true`. Model downloads happen during the build, while the resulting image sets the Hugging Face/Transformers offline flags. Build-time network access and runtime offline inference are distinct requirements.

**Release configuration.** The existing pipeline selects `aiur-global-builder` and requires the `aiursoft` project namespace. `LOCAL_DOCKER_USERNAME` and `LOCAL_DOCKER_PASSWORD` authenticate to `hub.aiursoft.com`. `DOCKER_USERNAME` and `DOCKER_PASSWORD` additionally authenticate to Docker Hub. Jobs set `IMAGE_NAME`, `CONTEXT`, optional `PLATFORMS` and optional `BUILD_ARGS`; both registries receive the same configured image name/tag. These are CI variables, not application runtime variables. Configure credentials through the existing private CI settings; do not put them in Dockerfiles or command examples.

## Development

Fetch the source and work in the repository root:

```bash
git clone https://gitlab.aiursoft.com/aiursoft/externalimages.git
cd externalimages
```

Build using the repository's standard build entry point:

```bash
docker buildx build --platform linux/amd64 --load \
  -t local/markitdown:dev ./MarkItDown
```

The build context is the selected image directory. This example loads one local image and does not publish it. Docker Buildx and access to the Dockerfile's base images and package/model sources are required; several contexts use internal base images even in ExternalImages. For multi-platform release builds, use the existing configured builder and the platform list from the release job.

Existing component checks include `python3 -m unittest discover -s WhisperX -p 'test_*.py' -v` and `python3 -m unittest discover -s ClickHouseAppLogErrorMonitor/tests -v`. The pipeline also performs its configured S360 npm review for AsrApi changes. These jobs use path-based rules; a root-README-only merge request may have no applicable pipeline jobs.

Publish the verified artifacts using the existing release workflow:

After a reviewed change reaches `master`, the matching deploy jobs build and push `latest` to both configured registries. Deploy jobs are limited by changes to their build context or `.gitlab-ci.yml`; a root README change alone does not request an image rebuild. The shared template contains branch-tag logic, but the current deploy rules only enable publication on `master`.

Image publication and deployment to a running Box are separate steps. Do not change shared registry credentials, builder configuration or publication rules as part of adding a documentation example.

## Documentation

- [ClickHouseAppLogErrorMonitor](ClickHouseAppLogErrorMonitor/README.md)
- [CloudflareAnalyticsCollector](CloudflareAnalyticsCollector/README.md)
- [FolderAgent](FolderAgent/README.md)
- [Shepherd140Fixed](Shepherd140Fixed/README.md)
- [WhisperX](WhisperX/README.md)

Keep each component's detailed configuration, authentication, storage, recovery and testing instructions in its existing document. This root README provides the collection-level entry point; it does not replace those documents.

## How to contribute

There are many ways to contribute to the project: logging bugs, submitting pull requests, reporting issues, and creating suggestions.

Even if you have push rights on the repository, you should create a personal fork and create feature branches there when you need them. This keeps the main repository clean and your workflow cruft out of sight.

We're also interested in your feedback on the future of this project. You can submit a suggestion or feature request through the issue tracker. To make this process more effective, we're asking that these include more information to help define them more clearly.

## License

This repository does not currently declare a collection-wide license in a root LICENSE file. Individual bundled projects and image layers retain their own upstream licenses and notices; inspect the selected component before redistribution. Do not assume one license applies to every image in this collection.
