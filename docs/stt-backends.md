# Choosing an STT backend

This worker speaks OpenAI's [`/v1/audio/transcriptions`](https://platform.openai.com/docs/api-reference/audio/createTranscription) protocol. Any backend that implements that endpoint will work — you only change `STT_BASE_URL` and `STT_MODEL`.

## OpenAI hosted Whisper

- **Pros:** zero self-hosting, proven quality, good latency.
- **Cons:** ~$0.006/min per transcribed audio. Your audio leaves your network.
- **Setup:**
  ```
  STT_BASE_URL=https://api.openai.com/v1
  STT_API_KEY=sk-...
  STT_MODEL=whisper-1
  ```

## speaches (self-hosted, GPU)

[speaches-ai/speaches](https://github.com/speaches-ai/speaches) is an OpenAI-compatible STT/TTS server that wraps `faster-whisper`. Runs on NVIDIA GPUs; models load into VRAM lazily and unload after an idle timeout.

- **Pros:** free at the per-minute level, private. Supports every `faster-whisper` and `faster-distil-whisper` model. Dynamic model switching (you can pick a different model per request).
- **Cons:** you manage the GPU.
- **Setup:**
  ```
  STT_BASE_URL=http://your-gpu-host:8000/v1
  STT_API_KEY=none
  STT_MODEL=Systran/faster-distil-whisper-small.en
  ```

### Model recommendations for speaches

| Model | VRAM (int8) | Best for |
|---|---:|---|
| `Systran/faster-distil-whisper-small.en` | ~0.5 GB | English-only meetings, fast, low-footprint |
| `Systran/faster-distil-whisper-medium.en` | ~1.5 GB | English-only, higher accuracy on fast speech / names |
| `Systran/faster-whisper-large-v3` | ~3.0 GB | Multilingual, best accuracy |
| `deepdml/faster-whisper-large-v3-turbo-ct2` | ~2.0 GB | Multilingual, faster than large-v3 with comparable quality |

Pick the smallest that meets your WER target — smaller models deliver faster captions and coexist more comfortably with other GPU workloads on the same host.

## faster-whisper-server (self-hosted, alternative)

[fedirz/faster-whisper-server](https://github.com/fedirz/faster-whisper-server) is similar to speaches. Interchangeable config:
```
STT_BASE_URL=http://your-host:8000/v1
STT_API_KEY=none
STT_MODEL=Systran/faster-distil-whisper-small.en
```

## Deepgram

Deepgram offers an [OpenAI-compatible endpoint](https://developers.deepgram.com/docs/openai-compatibility) that accepts the same request shape.

- **Pros:** very fast (sub-300ms), excellent multilingual coverage, $0.004/min.
- **Cons:** cloud service, per-minute fee.
- **Setup:**
  ```
  STT_BASE_URL=https://api.deepgram.com/v1
  STT_API_KEY=your-deepgram-key
  STT_MODEL=nova-2
  ```

## Cost comparison (per minute of audio)

| Backend | Model | Approx $/min |
|---|---|---:|
| OpenAI hosted | whisper-1 | $0.006 |
| Deepgram | nova-2 | $0.004 |
| speaches / faster-whisper-server | any (self-hosted) | $0.00 + electricity |

Streaming STT services like Deepgram Nova-2 give ~300 ms end-to-end latency. Self-hosted whisper via VAD chunking lands around 500 ms–1 s after an utterance ends. For captions on a meeting that's indistinguishable to users.
