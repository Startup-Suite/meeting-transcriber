# Architecture

## Components

```
┌──────────────────────────┐
│ LiveKit server            │
│  (your hosted SFU)        │
└──────┬──────────────┬─────┘
       │              │
       │              │ (1) worker registration
       │              │     over WebSocket
       │              ▼
       │       ┌────────────────────────────┐
       │       │  meeting-transcriber       │
       │       │                            │
       │       │  livekit-agents SDK        │
       │       │  ─ request_fnc             │ ← accepts/rejects based on ROOM_PATTERN
       │       │  ─ entrypoint              │ ← runs per-accepted room
       │       │  ─ Silero VAD              │ ← chunks utterances
       │       │  ─ openai.STT plugin       │ ← calls STT backend
       │       └──────────┬─────────────────┘
       │                  │ (3) HTTP POST /v1/audio/transcriptions
       │                  ▼
       │        ┌─────────────────────────┐
       │        │  STT backend            │
       │        │  (OpenAI-compatible)    │
       │        └──────────┬──────────────┘
       │                   │ (4) {"text": "..."}
       │                   │
       │  ◄────────────────┘
       │  (5) publish_transcription (LiveKit data channel)
       │
       ▼
 ┌──────────────────────┐
 │  meeting clients     │
 │  (Startup Suite core │ ← MeetingRoom JS hook consumes the
 │   + any other LK     │    transcription events and renders captions
 │   client)            │
 └──────────────────────┘

(2) Humans join the room → their audio tracks flow to the worker
    via subscribe → STT pipeline activates.
```

## Job lifecycle

1. **Worker startup** — `meeting-transcriber` process starts, registers with the LiveKit server over WebSocket, publishes its `agent_name` identity.
2. **Room created** — a human joins a room. LiveKit dispatches a job to the worker.
3. **Accept / reject** — `request_fnc` checks `ROOM_PATTERN`. Mismatches are rejected so another worker can handle them (or the job is dropped if no other worker matches).
4. **Connect** — on accept, `entrypoint` joins the room with identity `transcriber:{room_name}`. `auto_subscribe=AUDIO_ONLY`, so we don't pay bandwidth for video.
5. **Per-track pump** — for each audio track that becomes subscribed, a `TrackTranscriber` task starts. It feeds frames into a `StreamAdapter(stt, vad)` pipeline and drains `SpeechEvent`s.
6. **Publish** — each final segment becomes a `rtc.Transcription` published via `local_participant.publish_transcription`. LiveKit forwards the event to every client in the room over the data channel.
7. **Teardown** — when participants leave, their pumps are cancelled. When the room empties and `IDLE_TIMEOUT_S` passes, the worker disconnects (LiveKit re-dispatches if someone re-joins).

## Speaker attribution

Each audio track is owned by exactly one participant, so every transcription segment gets the publisher's `participant.identity` attached. No separate diarization step (pyannote etc.) is required for multi-speaker attribution.

## Coexistence with other agents

`ROOM_PATTERN` is the only knob. Example: a phone-conversation agent sets `ROOM_PATTERN=phone-*` and this transcriber sets `ROOM_PATTERN=space-*`. Both workers receive every job request, but each only accepts its own.

If multiple workers accept a job, LiveKit picks one at random.

## Persistence (out of scope here)

This worker produces live captions only. Persistent transcripts — for post-meeting summaries, search, etc. — are typically handled by your server receiving the LiveKit `transcription` webhook (e.g. in Startup Suite core, `LivekitWebhookController`). That path is independent of this worker.
