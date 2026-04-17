# meeting-transcriber

A [LiveKit Agents](https://docs.livekit.io/agents/) worker that transcribes meetings in real time using any [OpenAI-compatible](https://platform.openai.com/docs/api-reference/audio/createTranscription) speech-to-text backend.

Originally built for the Meetings integration in [Startup Suite](https://github.com/Startup-Suite/core) (see [ADR 0030](https://github.com/Startup-Suite/core/blob/main/docs/decisions/0030-meetings-livekit-voice-video.md)), but the worker itself is **generic** — point it at any LiveKit server and any OpenAI-compatible STT endpoint and it will subscribe to audio tracks in matching rooms, stream the audio through STT, and publish transcription segments back to the room using LiveKit's native [`Transcription`](https://docs.livekit.io/home/client/tracks/transcriptions/) API.

## What it does

```
                      ┌─────────────────────────────────┐
                      │           Your LiveKit server    │
                      └───┬──────────────────┬──────────┘
                          │                  │
              audio tracks │                  │ transcription segments
                          ▼                  │
    ┌──────────────────────────────┐         │
    │  meeting-transcriber worker  │         │
    │  (this project)              │         │
    │  ─ livekit-agents SDK        │         │
    │  ─ Silero VAD chunking       │         │
    │  ─ OpenAI-compat STT client  │         │
    └─────────┬────────────────────┘         │
              │ HTTP /v1/audio/transcriptions │
              ▼                                │
    ┌──────────────────────────────┐         │
    │  STT backend (pluggable)     │         │
    │  OpenAI Whisper hosted       │         │
    │  speaches (self-hosted GPU)  │         │
    │  faster-whisper-server       │         │
    │  Deepgram (OpenAI-compat)    │         │
    └──────────────────────────────┘         │
                                              ▼
                              ┌──────────────────────────┐
                              │  your meeting clients    │
                              │  (receive captions via   │
                              │   LiveKit Transcription) │
                              └──────────────────────────┘
```

Each audio track in a room is owned by exactly one participant, so speaker attribution comes for free — no diarization needed.

## Prerequisites

- A running [LiveKit](https://livekit.io/) server (cloud or self-hosted) with API key + secret
- An OpenAI-compatible STT endpoint. Any of:
  - [OpenAI Whisper (hosted)](https://platform.openai.com/docs/guides/speech-to-text) — easiest, pay per minute
  - [speaches](https://github.com/speaches-ai/speaches) — self-hosted, GPU, OpenAI-compatible
  - [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) — self-hosted alternative
  - [Deepgram](https://developers.deepgram.com/docs/openai-compatibility) — cloud, OpenAI-compatible endpoint
- Python 3.11+ (or just Docker)

## Quickstart — OpenAI hosted Whisper

Fastest path to a working transcriber. No self-hosting required.

```bash
git clone https://github.com/Startup-Suite/meeting-transcriber.git
cd meeting-transcriber
cp .env.example .env
# Edit .env:
#   LIVEKIT_URL=wss://your-livekit-server
#   LIVEKIT_API_KEY=...
#   LIVEKIT_API_SECRET=...
#   STT_BASE_URL=https://api.openai.com/v1
#   STT_API_KEY=sk-your-openai-key
#   STT_MODEL=whisper-1

docker compose up -d
```

Create a meeting in Startup Suite (or publish an audio track to any matching LiveKit room) and captions will appear in the client.

## Quickstart — self-hosted (speaches on GPU)

If you want to keep transcription off the OpenAI bill, run [speaches](https://github.com/speaches-ai/speaches) on a GPU host:

```yaml
# compose.yml on your GPU host
services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:latest-cuda
    ports: ["8000:8000"]
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - speaches-data:/home/ubuntu/.cache/huggingface/hub
    restart: unless-stopped

volumes:
  speaches-data:
```

Then point the transcriber at it:

```bash
STT_BASE_URL=http://your-gpu-host:8000/v1
STT_API_KEY=none              # speaches does not authenticate
STT_MODEL=Systran/faster-distil-whisper-small.en
```

`faster-distil-whisper-small.en` is a good default for English: ~500 MB VRAM, faster-than-real-time on mid-range GPUs, WER in the low single digits. For multilingual use `Systran/faster-whisper-large-v3` (~3 GB VRAM).

## Configuration

All config is environment-driven. See [`.env.example`](./.env.example) for the full list.

| Variable | Required | Description |
|---|---|---|
| `LIVEKIT_URL` | yes | WebSocket URL of your LiveKit server |
| `LIVEKIT_API_KEY` | yes | LiveKit API key (agent identity) |
| `LIVEKIT_API_SECRET` | yes | LiveKit API secret |
| `STT_BASE_URL` | yes | OpenAI-compatible base URL (must end at `/v1`) |
| `STT_API_KEY` | no | API key for STT backend (defaults to `none`) |
| `STT_MODEL` | yes | Model name the STT backend expects |
| `ROOM_PATTERN` | no | Glob for room names to join (default `*`). Use `space-*` for Startup Suite. |
| `IDLE_TIMEOUT_S` | no | Seconds of silence before disconnect (default `300`) |
| `EMPTY_GRACE_S` | no | Seconds after the last human leaves before the agent self-disconnects (default `20`; `0` disables) |
| `PERSIST_URL` | no | HTTP endpoint to POST final segments to (enables durable transcript storage; disabled when empty) |
| `PERSIST_TOKEN` | no | Bearer token sent with `PERSIST_URL` requests |
| `LOG_LEVEL` | no | Python log level (default `INFO`) |

### Room routing

`ROOM_PATTERN` lets multiple LiveKit agents coexist on the same server without fighting for jobs. A common setup is a phone-conversation agent (joins `phone-*`) plus this transcriber (joins `space-*`). Each agent only accepts jobs for its matching rooms.

The worker also registers with `agent_name="meeting-transcriber"`, so LiveKit clients that mint tokens with an explicit `roomConfig.agents` dispatch can route jobs directly to this worker without relying on pattern matching.

### Persistence

When `PERSIST_URL` is set, each final segment is POSTed as JSON to that endpoint alongside the usual `publish_transcription` data-channel broadcast. Body shape:

```json
{
  "room_sid": "RM_xxx",
  "room_name": "space-<uuid>",
  "segments": [
    {
      "participant_identity": "user:...",
      "speaker_name": "Alice",
      "text": "hello",
      "start_time": 0,
      "end_time": 0,
      "language": "en",
      "final": true
    }
  ]
}
```

Startup Suite's core exposes a compatible endpoint at `POST /api/meetings/segments`. The LiveKit data-channel publish is unaffected — live captions continue to work whether or not persistence is wired.

### Empty-room teardown

When the last human participant leaves, the agent waits `EMPTY_GRACE_S` seconds (default 20) and then disconnects. This triggers LiveKit's `room_finished` webhook so downstream systems can finalize transcripts and run post-processing (e.g., LLM summaries) promptly, rather than waiting for LiveKit's much longer `empty_timeout`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env   # fill in values
meeting-transcriber dev
```

`meeting-transcriber dev` (a LiveKit Agents CLI alias for `start` in dev mode) runs the worker with hot reload and verbose logging.

### Tests

```bash
pytest
```

### Lint / typecheck

```bash
ruff check .
mypy src
```

## Deployment

### Docker

```bash
docker build -t meeting-transcriber .
docker run --env-file .env --restart=unless-stopped meeting-transcriber
```

### Pre-built image (once CI publishes)

```bash
docker pull ghcr.io/startup-suite/meeting-transcriber:latest
```

### Podman + systemd (quadlet)

See [`deploy/meeting-transcriber.container.example`](./deploy/meeting-transcriber.container.example) for a generic Podman Quadlet file. Drop it into `~/.config/containers/systemd/`, supply an env file, and `systemctl --user daemon-reload && systemctl --user enable --now meeting-transcriber.service`.

## How it fits with Startup Suite core

Core's `PlatformWeb.ChatLive.MeetingHooks` mints LiveKit JWTs for human participants when they click **Meet**. This worker registers as a LiveKit Agent, receives a job request whenever a matching room gets its first human participant, joins, subscribes to audio tracks, and publishes transcription segments. Core's client-side `MeetingRoom` JS hook consumes those segments and renders a rolling caption overlay — no server-side integration required.

Captions are also persisted by core's `LivekitWebhookController` for post-meeting summaries — that path is independent of this worker (it's a webhook from LiveKit itself) and keeps working whether you run this transcriber, a different one, or none at all.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
