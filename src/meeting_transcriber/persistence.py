"""Optional HTTP sink for final transcription segments.

The agent always publishes transcriptions to the LiveKit room's data
channel so every client renders them in real time. Callers that also
want durable storage can set `PERSIST_URL` / `PERSIST_TOKEN` to forward
final segments to a backend — Startup Suite exposes one at
`POST /api/meetings/segments`.

This module intentionally swallows errors: persistence is best-effort
and must never block the in-room transcription pump.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("meeting_transcriber.persistence")


class SegmentSink:
    """Buffered HTTP poster for final transcription segments."""

    def __init__(self, url: str, token: str, room_sid: str, room_name: str) -> None:
        self._url = url
        self._token = token
        self._room_sid = room_sid
        self._room_name = room_name
        self._client = httpx.AsyncClient(timeout=5.0)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def post(self, segment: dict) -> None:
        if not self.enabled:
            return

        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"

        body = {
            "room_sid": self._room_sid,
            "room_name": self._room_name,
            "segments": [segment],
        }

        try:
            resp = await self._client.post(self._url, json=body, headers=headers)
            if resp.status_code >= 300:
                logger.warning(
                    "persist POST %s returned %d: %s",
                    self._url,
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception:
            logger.exception("persist POST %s failed", self._url)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
