"""LiveKit Agents worker entrypoint.

Subscribes to audio tracks in matching rooms, transcribes via an
OpenAI-compatible STT backend, and publishes transcription segments
back to the room using LiveKit's Transcription API so every client
receives them (including Startup Suite's `MeetingRoom` JS hook).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, JobRequest, WorkerOptions, cli
from livekit.agents import stt as stt_module
from livekit.plugins import openai, silero

from .config import load_config, room_matches

logger = logging.getLogger("meeting_transcriber")


@dataclass
class TrackTranscriber:
    """Per-track transcription pump.

    One of these runs for each audio track in the room. It pipes the
    track's audio through Silero VAD (to chunk utterances) and then to
    the STT backend. Final transcripts are published to the room with
    the participant's identity attached so clients know who spoke.
    """

    ctx: JobContext
    stt: stt_module.STT
    vad: silero.VAD
    track: rtc.Track
    participant: rtc.RemoteParticipant

    async def run(self) -> None:
        audio_stream = rtc.AudioStream(self.track)
        stt_stream = stt_module.StreamAdapter(stt=self.stt, vad=self.vad).stream()

        # forward audio frames
        forward_task = asyncio.create_task(self._forward_audio(audio_stream, stt_stream))

        try:
            async for ev in stt_stream:
                if ev.type == stt_module.SpeechEventType.FINAL_TRANSCRIPT:
                    alt = ev.alternatives[0] if ev.alternatives else None
                    if alt and alt.text:
                        await self._publish(alt.text, final=True)
                elif ev.type == stt_module.SpeechEventType.INTERIM_TRANSCRIPT:
                    alt = ev.alternatives[0] if ev.alternatives else None
                    if alt and alt.text:
                        await self._publish(alt.text, final=False)
        except Exception:
            logger.exception(
                "stt stream failed for %s", self.participant.identity
            )
        finally:
            forward_task.cancel()

    async def _forward_audio(self, audio_stream, stt_stream) -> None:
        try:
            async for frame_event in audio_stream:
                stt_stream.push_frame(frame_event.frame)
        except Exception:
            logger.exception(
                "audio forward failed for %s", self.participant.identity
            )
        finally:
            stt_stream.end_input()

    async def _publish(self, text: str, *, final: bool) -> None:
        segment = rtc.TranscriptionSegment(
            id=f"SG_{uuid.uuid4().hex[:12]}",
            text=text,
            start_time=0,
            end_time=0,
            language="en",
            final=final,
        )
        tx = rtc.Transcription(
            participant_identity=self.participant.identity,
            track_sid=self.track.sid,
            segments=[segment],
        )
        try:
            await self.ctx.room.local_participant.publish_transcription(tx)
        except Exception:
            logger.exception(
                "publish_transcription failed for %s", self.participant.identity
            )


async def request_fnc(req: JobRequest) -> None:
    """Accept/reject jobs based on the configured room pattern.

    Called by the livekit-agents worker runtime before spinning up
    `entrypoint` for a given room. Keeps this worker from grabbing rooms
    that belong to a different agent (e.g. phone-call agents).
    """
    cfg = load_config()
    if room_matches(req.room.name, cfg.room_pattern):
        await req.accept(identity=_agent_identity(req.room.name))
    else:
        await req.reject()


async def entrypoint(ctx: JobContext) -> None:
    cfg = load_config()
    _configure_logging(cfg.log_level)

    logger.info(
        "joining room=%s pattern=%s stt=%s model=%s",
        ctx.room.name,
        cfg.room_pattern,
        cfg.stt_base_url,
        cfg.stt_model,
    )

    stt = openai.STT(
        base_url=cfg.stt_base_url,
        api_key=cfg.stt_api_key,
        model=cfg.stt_model,
    )
    vad = silero.VAD.load()

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    transcribers: dict[str, asyncio.Task] = {}

    def _start(track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if participant.identity in transcribers:
            return
        runner = TrackTranscriber(
            ctx=ctx, stt=stt, vad=vad, track=track, participant=participant
        )
        transcribers[participant.identity] = asyncio.create_task(runner.run())

    @ctx.room.on("track_subscribed")
    def _on_sub(
        track: rtc.Track,
        _pub: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        _start(track, participant)

    @ctx.room.on("participant_disconnected")
    def _on_leave(participant: rtc.RemoteParticipant) -> None:
        task = transcribers.pop(participant.identity, None)
        if task:
            task.cancel()

    # pick up any tracks that were already subscribed before our handlers ran
    for participant in ctx.room.remote_participants.values():
        for pub in participant.track_publications.values():
            if pub.track:
                _start(pub.track, participant)


def _agent_identity(room_name: str) -> str:
    return f"transcriber:{room_name}"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    # Load config once at startup to fail fast on bad env.
    load_config()
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=request_fnc,
        )
    )


if __name__ == "__main__":
    main()
