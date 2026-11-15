"""RTP media relay between the VTO (SIP call) and local RTSP viewers."""
from __future__ import annotations

import asyncio
import logging
import random
import struct
import time
from typing import Callable

from .sip import MediaPlan

_LOGGER = logging.getLogger(__name__)

SILENCE_PCMA = b"\xd5" * 160


class _RtpSocket(asyncio.DatagramProtocol):
    def __init__(self, on_packet: Callable[[bytes, tuple[str, int]], None]) -> None:
        self._on_packet = on_packet
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self._on_packet(data, addr)


class MediaRelay:
    """Owns the RTP sockets; forwards VTO media to a sink and mic audio to the VTO."""

    def __init__(
        self,
        audio_port: int,
        video_port: int,
        sink: Callable[[str, bytes], None],
        mic_source: Callable[[int], bytes | None] | None = None,
    ) -> None:
        self.mic_source = mic_source  # returns n mixed microphone samples (A-law) or None
        self.audio_port = audio_port
        self.video_port = video_port
        self._sink = sink
        self._audio: _RtpSocket | None = None
        self._video: _RtpSocket | None = None
        self._plan: MediaPlan | None = None
        self._tx_task: asyncio.Task | None = None
        self._seq = random.randint(0, 0xFFFF)
        self._ts = random.randint(0, 0x7FFFFFFF)
        self._ssrc = random.randint(1, 0x7FFFFFFF)
        self._mic_until = 0.0
        self._mic_buf = bytearray()
        self.stats = {"audio_rx": 0, "video_rx": 0, "audio_tx": 0, "mic_rx": 0}

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        _, self._audio = await loop.create_datagram_endpoint(
            lambda: _RtpSocket(self._on_audio), local_addr=("0.0.0.0", self.audio_port)
        )
        _, self._video = await loop.create_datagram_endpoint(
            lambda: _RtpSocket(self._on_video), local_addr=("0.0.0.0", self.video_port)
        )

    async def stop(self) -> None:
        self.end()
        for s in (self._audio, self._video):
            if s and s.transport:
                s.transport.close()

    @property
    def active(self) -> bool:
        return self._plan is not None

    def begin(self, plan: MediaPlan) -> None:
        """Start relaying for a call."""
        self._plan = plan
        self.stats = {"audio_rx": 0, "video_rx": 0, "audio_tx": 0, "mic_rx": 0}
        self._mic_buf.clear()
        if self._video and self._video.transport and plan.video_port:
            # open the path for symmetric RTP / NAT-less firewalls
            self._video.transport.sendto(struct.pack("!BBHII", 0x80, plan.video_pt or 96, 0, 0, self._ssrc), (plan.remote_ip, plan.video_port))
        if self._tx_task is None or self._tx_task.done():
            self._tx_task = asyncio.get_running_loop().create_task(self._tx_loop())
        _LOGGER.debug("Media started: %s", plan)

    def end(self) -> None:
        if self._plan is not None:
            _LOGGER.debug("Media ended, stats %s", self.stats)
        self._plan = None
        if self._tx_task:
            self._tx_task.cancel()
            self._tx_task = None

    # -------------------------------------------------------------- RX

    def _on_audio(self, data: bytes, addr) -> None:
        plan = self._plan
        if plan is None or addr[0] != plan.remote_ip or len(data) < 12:
            return
        pt = data[1] & 0x7F
        if pt != plan.audio_pt:
            return  # DTMF or comfort noise
        self.stats["audio_rx"] += 1
        self._sink("audio", data)

    def _on_video(self, data: bytes, addr) -> None:
        plan = self._plan
        if plan is None or addr[0] != plan.remote_ip or len(data) < 12:
            return
        if (data[1] & 0x7F) >= 72 and (data[1] & 0x7F) <= 76:
            return  # RTCP on the RTP port
        self.stats["video_rx"] += 1
        self._sink("video", data)

    # -------------------------------------------------------------- TX

    def push_mic(self, payload: bytes) -> None:
        """PCMA payload from a viewer's microphone."""
        if self._plan is None:
            return
        self.stats["mic_rx"] += 1
        self._mic_buf += payload
        if len(self._mic_buf) > 8000:  # keep at most 1 s of latency
            del self._mic_buf[: len(self._mic_buf) - 1600]
        self._mic_until = time.monotonic() + 0.5

    async def _tx_loop(self) -> None:
        """Send one 20 ms PCMA frame every 20 ms: mic audio or silence."""
        loop = asyncio.get_running_loop()
        next_t = loop.time()
        try:
            while True:
                plan = self._plan
                if plan is None:
                    return
                frame = self.mic_source(160) if self.mic_source else None
                if frame is not None:
                    self.stats["mic_rx"] += 1
                else:
                    frame = SILENCE_PCMA
                hdr = struct.pack("!BBHII", 0x80, plan.audio_pt, self._seq & 0xFFFF, self._ts & 0xFFFFFFFF, self._ssrc)
                self._seq += 1
                self._ts += 160
                if self._audio and self._audio.transport and plan.audio_port:
                    self._audio.transport.sendto(hdr + frame, (plan.remote_ip, plan.audio_port))
                    self.stats["audio_tx"] += 1
                next_t += 0.02
                delay = next_t - loop.time()
                if delay < -0.2:  # we fell behind (loop busy): resync
                    next_t = loop.time()
                    delay = 0
                await asyncio.sleep(max(0, delay))
        except asyncio.CancelledError:
            pass
