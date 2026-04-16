"""Unit tests for env-driven configuration."""

from __future__ import annotations

import pytest

from meeting_transcriber.config import load_config, room_matches


@pytest.fixture
def required_env(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://lk.example.com")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret")
    monkeypatch.setenv("STT_BASE_URL", "http://stt.example.com/v1")
    monkeypatch.setenv("STT_MODEL", "whisper-1")


def test_load_config_applies_defaults(monkeypatch, required_env):
    monkeypatch.delenv("STT_API_KEY", raising=False)
    monkeypatch.delenv("ROOM_PATTERN", raising=False)
    monkeypatch.delenv("IDLE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    cfg = load_config()

    assert cfg.livekit_url == "wss://lk.example.com"
    assert cfg.stt_api_key == "none"
    assert cfg.room_pattern == "*"
    assert cfg.idle_timeout_s == 300
    assert cfg.log_level == "INFO"


def test_load_config_reads_overrides(monkeypatch, required_env):
    monkeypatch.setenv("STT_API_KEY", "sk-override")
    monkeypatch.setenv("ROOM_PATTERN", "space-*")
    monkeypatch.setenv("IDLE_TIMEOUT_S", "120")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    cfg = load_config()

    assert cfg.stt_api_key == "sk-override"
    assert cfg.room_pattern == "space-*"
    assert cfg.idle_timeout_s == 120
    assert cfg.log_level == "DEBUG"  # uppercased


@pytest.mark.parametrize(
    "required_var",
    ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "STT_BASE_URL", "STT_MODEL"],
)
def test_load_config_fails_fast_on_missing_var(monkeypatch, required_env, required_var):
    monkeypatch.delenv(required_var, raising=False)

    with pytest.raises(RuntimeError, match=required_var):
        load_config()


@pytest.mark.parametrize(
    "name,pattern,expected",
    [
        ("space-abc", "space-*", True),
        ("space-abc", "*", True),
        ("phone-call-1", "space-*", False),
        ("phone-call-1", "phone-*", True),
        ("phone-call-1", "*", True),
        ("anything", "space-*", False),
    ],
)
def test_room_matches(name, pattern, expected):
    assert room_matches(name, pattern) is expected
