"""Environment-driven configuration."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # LiveKit server (agent connects as a worker here)
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # OpenAI-compatible STT backend (OpenAI hosted Whisper, speaches,
    # faster-whisper-server, Deepgram, …). base_url should terminate at the
    # "/v1" root — the SDK appends the /audio/transcriptions path.
    stt_base_url: str
    stt_api_key: str
    stt_model: str

    # Room-name glob pattern. Jobs for rooms not matching are rejected so
    # this worker can coexist with other LiveKit agents on the same server.
    # Default "*" means "join every room" — users integrating with Suite
    # core will typically set this to "space-*" to match Suite's naming.
    room_pattern: str

    # Inactivity timeout (seconds) before the worker disconnects from an
    # empty room. Keeps GPU idle when no one is talking.
    idle_timeout_s: int

    log_level: str

    # Optional: push final transcription segments to a Suite-style HTTP
    # endpoint for durable persistence. The agent always publishes via
    # LiveKit's data channel (for live captions); this is an additional
    # out-of-band sink. Disabled when `persist_url` is empty.
    persist_url: str
    persist_token: str

    # Grace period (seconds) after the last human participant leaves before
    # the agent disconnects from the room. Zero disables — the agent stays
    # until the LiveKit idle_timeout triggers teardown.
    empty_grace_s: int


def load_config() -> Config:
    """Load configuration from env. `.env` is read if present (dev convenience)."""
    load_dotenv()

    return Config(
        livekit_url=_require("LIVEKIT_URL"),
        livekit_api_key=_require("LIVEKIT_API_KEY"),
        livekit_api_secret=_require("LIVEKIT_API_SECRET"),
        stt_base_url=_require("STT_BASE_URL"),
        stt_api_key=os.getenv("STT_API_KEY", "none"),
        stt_model=_require("STT_MODEL"),
        room_pattern=os.getenv("ROOM_PATTERN", "*"),
        idle_timeout_s=int(os.getenv("IDLE_TIMEOUT_S", "300")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        persist_url=os.getenv("PERSIST_URL", "").strip(),
        persist_token=os.getenv("PERSIST_TOKEN", "").strip(),
        empty_grace_s=int(os.getenv("EMPTY_GRACE_S", "20")),
    )


def room_matches(room_name: str, pattern: str) -> bool:
    """Glob-match a room name against the configured pattern."""
    return fnmatch.fnmatch(room_name, pattern)


def _require(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise RuntimeError(f"environment variable {key} is required")
    return value
