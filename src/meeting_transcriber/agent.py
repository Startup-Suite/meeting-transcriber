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
from .persistence import SegmentSink

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
    sink: SegmentSink | None = None

    async def run(self) -> None:
        logger.info(
            "track pump starting identity=%s track_sid=%s",
            self.participant.identity,
            self.track.sid,
        )
        audio_stream = rtc.AudioStream(self.track)
        stt_stream = stt_module.StreamAdapter(stt=self.stt, vad=self.vad).stream()

        # forward audio frames
        forward_task = asyncio.create_task(self._forward_audio(audio_stream, stt_stream))

        try:
            async for ev in stt_stream:
                if ev.type == stt_module.SpeechEventType.FINAL_TRANSCRIPT:
                    alt = ev.alternatives[0] if ev.alternatives else None
                    if alt and alt.text:
                        logger.info(
                            "FINAL %s: %s", self.participant.identity, alt.text
                        )
                        await self._publish(alt.text, final=True)
                        if self.sink:
                            await self.sink.post(
                                {
                                    "participant_identity": self.participant.identity,
                                    "speaker_name": self.participant.name
                                    or self.participant.identity,
                                    "text": alt.text,
                                    "start_time": 0,
                                    "end_time": 0,
                                    "language": "en",
                                    "final": True,
                                }
                            )
                elif ev.type == stt_module.SpeechEventType.INTERIM_TRANSCRIPT:
                    alt = ev.alternatives[0] if ev.alternatives else None
                    if alt and alt.text:
                        logger.debug(
                            "interim %s: %s", self.participant.identity, alt.text
                        )
                        await self._publish(alt.text, final=False)
        except Exception:
            logger.exception(
                "stt stream failed for %s", self.participant.identity
            )
        finally:
            forward_task.cancel()

    async def _forward_audio(self, audio_stream, stt_stream) -> None:
        frame_count = 0
        try:
            async for frame_event in audio_stream:
                if frame_count == 0:
                    logger.info(
                        "first audio frame from %s", self.participant.identity
                    )
                frame_count += 1
                if frame_count % 500 == 0:
                    logger.debug(
                        "forwarded %d frames from %s",
                        frame_count,
                        self.participant.identity,
                    )
                stt_stream.push_frame(frame_event.frame)
        except Exception:
            logger.exception(
                "audio forward failed for %s", self.participant.identity
            )
        finally:
            logger.info(
                "audio pump done for %s (%d frames)",
                self.participant.identity,
                frame_count,
            )
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
        await req.accept(
            identity=_agent_identity(req.room.name),
            name="Transcription",
        )
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
    # Room.sid is a coroutine in livekit-rtc >=1.x — await it once on connect.
    room_sid = await ctx.room.sid
    logger.info(
        "connected as identity=%s room_sid=%s remote_participants=%d",
        ctx.room.local_participant.identity,
        room_sid,
        len(ctx.room.remote_participants),
    )

    sink = SegmentSink(
        url=cfg.persist_url,
        token=cfg.persist_token,
        room_sid=room_sid,
        room_name=ctx.room.name,
    )
    if sink.enabled:
        logger.info("segment persistence enabled → %s", cfg.persist_url)

    transcribers: dict[str, asyncio.Task] = {}
    empty_timer: asyncio.TimerHandle | None = None

    def _cancel_empty_timer() -> None:
        nonlocal empty_timer
        if empty_timer is not None:
            empty_timer.cancel()
            empty_timer = None

    def _schedule_disconnect_if_empty() -> None:
        """If no humans remain, schedule a disconnect after the grace period."""
        nonlocal empty_timer
        if cfg.empty_grace_s <= 0:
            return
        humans = [
            p
            for p in ctx.room.remote_participants.values()
            if not p.identity.startswith("transcriber:")
        ]
        if humans:
            _cancel_empty_timer()
            return
        if empty_timer is not None:
            return
        logger.info(
            "room empty — disconnecting in %ds unless someone rejoins",
            cfg.empty_grace_s,
        )
        loop = asyncio.get_running_loop()
        empty_timer = loop.call_later(
            cfg.empty_grace_s,
            lambda: asyncio.create_task(_graceful_disconnect()),
        )

    async def _graceful_disconnect() -> None:
        logger.info("grace elapsed, agent disconnecting from empty room")
        try:
            await ctx.room.disconnect()
        except Exception:
            logger.exception("disconnect failed")

    def _start(track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            logger.debug(
                "skipping non-audio track kind=%s from %s",
                track.kind,
                participant.identity,
            )
            return
        if participant.identity in transcribers:
            logger.debug(
                "already have pump for %s, skipping", participant.identity
            )
            return
        logger.info(
            "starting pump for %s (track_sid=%s)",
            participant.identity,
            track.sid,
        )
        runner = TrackTranscriber(
            ctx=ctx,
            stt=stt,
            vad=vad,
            track=track,
            participant=participant,
            sink=sink,
        )
        transcribers[participant.identity] = asyncio.create_task(runner.run())

    @ctx.room.on("track_subscribed")
    def _on_sub(
        track: rtc.Track,
        _pub: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        logger.info(
            "track_subscribed identity=%s kind=%s sid=%s",
            participant.identity,
            track.kind,
            track.sid,
        )
        _start(track, participant)

    @ctx.room.on("participant_connected")
    def _on_join(participant: rtc.RemoteParticipant) -> None:
        logger.info("participant_connected identity=%s", participant.identity)
        _cancel_empty_timer()

    @ctx.room.on("participant_disconnected")
    def _on_leave(participant: rtc.RemoteParticipant) -> None:
        logger.info("participant_disconnected identity=%s", participant.identity)
        task = transcribers.pop(participant.identity, None)
        if task:
            task.cancel()
        _schedule_disconnect_if_empty()

    # pick up any tracks that were already subscribed before our handlers ran
    for participant in ctx.room.remote_participants.values():
        logger.info(
            "scanning existing participant=%s tracks=%d",
            participant.identity,
            len(participant.track_publications),
        )
        for pub in participant.track_publications.values():
            logger.info(
                "  pub sid=%s kind=%s subscribed=%s track?=%s",
                pub.sid,
                pub.kind,
                pub.subscribed,
                pub.track is not None,
            )
            if pub.track:
                _start(pub.track, participant)

    # If the agent joined a room that's already empty for some reason, kick
    # off the grace timer immediately so we don't sit idle forever.
    _schedule_disconnect_if_empty()


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
            agent_name="meeting-transcriber",
        )
    )


if __name__ == "__main__":
    main()
