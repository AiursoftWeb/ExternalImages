# WhisperX service

## Resident model

Set `WHISPERX_PRELOAD=true` on the WhisperX container to load the configured
`WHISPERX_MODEL` in its inference process before the HTTP application starts.
Startup fails if the model cannot be loaded; the service never reports healthy
with an unloaded model in this mode. Without this opt-in, existing deployments
keep their on-demand loading behavior.

The model remains loaded between requests. Cancelling an active transcription
still terminates its inference process promptly, then restores the configured
model in a background task. Health returns HTTP 503 and transcriptions are not
accepted until a model is resident again. A dead inference process is also
unhealthy, so the container supervisor can restart and preload it.

Preloading exposes the resident weight allocation for capacity planning; it
is not a GPU memory cap or a reservation of peak transcription workspace.
Budget simultaneous ASR, chat, embeddings, and speech peaks before enabling
it on a shared GPU. Startup preloading does not transcribe synthetic audio.

