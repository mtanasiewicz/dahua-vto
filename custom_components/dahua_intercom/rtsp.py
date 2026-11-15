"""Small RTSP server (TCP interleaved) with an ONVIF audio backchannel.

go2rtc (WebRTC Camera / Home Assistant) connects here, receives the door
station's H.264 video and PCMA audio, and sends the browser microphone back
as PCMA on the backchannel track.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import struct
import time
from typing import Callable

from .audio_mix import Speaker, decode, encode_alaw, mix_into

_LOGGER = logging.getLogger(__name__)

TRACK_VIDEO, TRACK_AUDIO, TRACK_BACK = 0, 1, 2
GOP_MAX_PACKETS = 1500  # ~2 s of 720p H.264


class _Codec:
    """What we announce for one track (mirrors the upstream VTO stream)."""

    def __init__(self, pt: int, rtpmap: str, fmtp: str = "") -> None:
        self.pt = pt
        self.rtpmap = rtpmap
        self.fmtp = fmtp

    @property
    def clock(self) -> int:
        try:
            return int(self.rtpmap.split("/")[1])
        except (IndexError, ValueError):
            return 90000


class _TrackState:
    """Rewrites RTP headers so viewers see one continuous stream across calls."""

    def __init__(self, track: int, codec: _Codec) -> None:
        self.track = track
        self.codec = codec
        self.ssrc = random.randint(1, 0x7FFFFFFF)
        self.seq = random.randint(0, 0xFFFF)
        self.src_ssrc: int | None = None
        self.ts_offset = 0
        self.last_out_ts = random.randint(0, 0x7FFFFFFF)
        self.last_wall = time.monotonic()

    def rewrite(self, pkt: bytes) -> bytes:
        b = bytearray(pkt)
        in_ts, in_ssrc = struct.unpack_from("!II", b, 4)
        if in_ssrc != self.src_ssrc:
            gap = int((time.monotonic() - self.last_wall) * self.codec.clock)
            self.ts_offset = (self.last_out_ts + max(gap, 1) - in_ts) & 0xFFFFFFFF
            self.src_ssrc = in_ssrc
        out_ts = (in_ts + self.ts_offset) & 0xFFFFFFFF
        self.seq = (self.seq + 1) & 0xFFFF
        b[1] = (b[1] & 0x80) | self.codec.pt
        struct.pack_into("!HII", b, 2, self.seq, out_ts, self.ssrc)
        self.last_out_ts = out_ts
        self.last_wall = time.monotonic()
        return bytes(b)


class _Client:
    def __init__(self, server: "RtspServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.session = str(random.randint(10**9, 10**10))
        self.channels: dict[int, int] = {}  # track -> interleaved RTP channel
        self.playing = False
        self.backchannel = False
        self.back_channels: set[int] = set()  # every channel offered for track 2
        self.frames: dict[int, int] = {}  # interleaved channel -> frames received
        self.speaker = Speaker()  # this viewer's microphone
        self.peer = writer.get_extra_info("peername")

    def _on_frame(self, ch: int, data: bytes) -> None:
        """Interleaved frame sent by the client (backchannel RTP or RTCP reports)."""
        n = self.frames.get(ch, 0) + 1
        self.frames[ch] = n
        if n == 1 or n % 500 == 0:
            _LOGGER.debug(
                "RTSP %s: frame #%d on channel %d (%d bytes, b0=%#x b1=%#x), back=%s",
                self.peer, n, ch, len(data), data[0] if data else 0, data[1] if len(data) > 1 else 0,
                sorted(self.back_channels),
            )
        if len(data) <= 12 or data[0] >> 6 != 2:
            return
        pt = data[1] & 0x7F
        if 72 <= pt <= 76:
            return  # RTCP (PT byte 200-204)
        if ch not in self.back_channels and pt not in (0, 8):
            return
        self.speaker.feed(decode(_rtp_payload(data), pt))

    # ------------------------------------------------------------ I/O

    def send_rtp(self, track: int, pkt: bytes) -> None:
        ch = self.channels.get(track)
        if ch is None or self.writer.is_closing():
            return
        transport = self.writer.transport
        if track == TRACK_VIDEO and transport.get_write_buffer_size() > 4_000_000:
            return  # slow viewer: drop video rather than buffer forever
        self.writer.write(b"$" + bytes([ch]) + struct.pack("!H", len(pkt)) + pkt)

    async def run(self) -> None:
        try:
            while True:
                first = await self.reader.readexactly(1)
                if first == b"$":
                    hdr = await self.reader.readexactly(3)
                    ch, size = hdr[0], struct.unpack("!H", hdr[1:])[0]
                    data = await self.reader.readexactly(size)
                    self._on_frame(ch, data)
                    continue
                head = first + await self.reader.readuntil(b"\r\n\r\n")
                await self._handle(head.decode(errors="replace"))
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("RTSP client error")
        finally:
            _LOGGER.debug("RTSP %s closed, frames per channel: %s", self.peer, self.frames)
            self.writer.close()
            self.server.remove(self)

    async def _handle(self, head: str) -> None:
        lines = head.split("\r\n")
        method, url = (lines[0].split() + ["", ""])[:2]
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if length:
            await self.reader.readexactly(length)
        cseq = headers.get("cseq", "0")
        _LOGGER.debug("RTSP %s %s from %s", method, url, self.peer)

        if method == "OPTIONS":
            self._reply(cseq, "Public: OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER\r\n")
        elif method == "DESCRIBE":
            self.backchannel = "backchannel" in headers.get("require", "")
            body = self.server.sdp(self.backchannel)
            base = url if url.endswith("/") else url + "/"
            self._reply(cseq, f"Content-Base: {base}\r\nContent-Type: application/sdp\r\n", body)
        elif method == "SETUP":
            track = _track_from_url(url)
            transport = headers.get("transport", "")
            if track is None or "interleaved=" not in transport:
                self._reply(cseq, "", status="461 Unsupported Transport")
                return
            ch = int(transport.split("interleaved=")[1].split("-")[0].split(";")[0])
            self.channels[track] = ch
            if track == TRACK_BACK:
                self.back_channels.add(ch)
            _LOGGER.debug("RTSP SETUP track %d transport=%r -> channel %d", track, transport, ch)
            self._reply(
                cseq,
                f"Transport: RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}\r\nSession: {self.session};timeout=60\r\n",
            )
        elif method == "PLAY":
            self._reply(cseq, f"Session: {self.session}\r\nRange: npt=0.000-\r\n")
            if not self.playing:
                self.playing = True
                # start with the cached GOP so the picture appears at once
                for pkt in self.server.gop:
                    self.send_rtp(TRACK_VIDEO, pkt)
                self.server.client_changed()
        elif method == "PAUSE":
            self._reply(cseq, f"Session: {self.session}\r\n")
        elif method in ("GET_PARAMETER", "SET_PARAMETER"):
            self._reply(cseq, f"Session: {self.session}\r\n")
        elif method == "TEARDOWN":
            self._reply(cseq, f"Session: {self.session}\r\n")
            self.writer.close()
        else:
            self._reply(cseq, "", status="405 Method Not Allowed")

    def _reply(self, cseq: str, extra: str, body: str = "", status: str = "200 OK") -> None:
        msg = f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\nServer: HA-DahuaIntercom\r\n{extra}"
        msg += f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
        self.writer.write(msg.encode())


def _track_from_url(url: str) -> int | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.startswith("trackID="):
        try:
            t = int(tail[8:])
        except ValueError:
            return None
        return t if t in (TRACK_VIDEO, TRACK_AUDIO, TRACK_BACK) else None
    return None


def _rtp_payload(pkt: bytes) -> bytes:
    cc = pkt[0] & 0x0F
    off = 12 + cc * 4
    if pkt[0] & 0x10 and len(pkt) >= off + 4:
        off += 4 + struct.unpack_from("!H", pkt, off + 2)[0] * 4
    end = len(pkt)
    if pkt[0] & 0x20:
        end -= pkt[-1]
    return pkt[off:end]


class RtspServer:
    """Serves rtsp://host:port/<anything> with the current call's media."""

    def __init__(self, host: str, port: int, on_clients: Callable[[int], None]) -> None:
        self.host = host
        self.port = port
        self._on_clients = on_clients
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[_Client] = set()
        self._codecs = {TRACK_VIDEO: _Codec(96, "H264/90000"), TRACK_AUDIO: _Codec(8, "PCMA/8000")}
        self._tracks = {t: _TrackState(t, c) for t, c in self._codecs.items()}
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self.gop: list[bytes] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for c in list(self._clients):
            c.writer.close()

    @property
    def viewers(self) -> int:
        return sum(1 for c in self._clients if c.playing)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(self, reader, writer)
        self._clients.add(client)
        asyncio.get_running_loop().create_task(client.run())

    def remove(self, client: _Client) -> None:
        if client in self._clients:
            self._clients.discard(client)
            if client.playing:
                self.client_changed()

    def client_changed(self) -> None:
        self._on_clients(self.viewers)

    def set_upstream(self, medias) -> None:
        """Mirror the codecs of the upstream (VTO RTSP) stream."""
        for m in medias:
            track = TRACK_VIDEO if m.kind == "video" else TRACK_AUDIO
            codec = self._codecs[track]
            changed = (codec.pt, codec.rtpmap) != (m.pt, m.rtpmap)
            codec.pt, codec.rtpmap, codec.fmtp = m.pt, m.rtpmap, m.fmtp
            if changed:
                _LOGGER.info("RTSP %s track now %s (pt %d)", m.kind, m.rtpmap, m.pt)
                self.gop.clear()

    def reset_cache(self) -> None:
        self.gop.clear()

    def sdp(self, backchannel: bool) -> str:
        v, a = self._codecs[TRACK_VIDEO], self._codecs[TRACK_AUDIO]
        fmtp = v.fmtp or "packetization-mode=1"
        if v.rtpmap.upper().startswith("H264") and self._sps and self._pps and "sprop-parameter-sets" not in fmtp:
            pli = self._sps[1:4].hex()
            fmtp += f";profile-level-id={pli};sprop-parameter-sets={base64.b64encode(self._sps).decode()},{base64.b64encode(self._pps).decode()}"
        lines = [
            "v=0",
            f"o=- {int(time.time())} 1 IN IP4 {self.host}",
            "s=Dahua Intercom",
            "t=0 0",
            "a=control:*",
            "a=range:npt=now-",
            f"m=video 0 RTP/AVP {v.pt}",
            "c=IN IP4 0.0.0.0",
            f"a=rtpmap:{v.pt} {v.rtpmap}",
            f"a=fmtp:{v.pt} {fmtp}",
            "a=recvonly",
            "a=control:trackID=0",
            f"m=audio 0 RTP/AVP {a.pt}",
            "c=IN IP4 0.0.0.0",
            f"a=rtpmap:{a.pt} {a.rtpmap}",
            *([f"a=fmtp:{a.pt} {a.fmtp}"] if a.fmtp else []),
            "a=recvonly",
            "a=control:trackID=1",
        ]
        if backchannel:
            lines += [
                "m=audio 0 RTP/AVP 8",
                "c=IN IP4 0.0.0.0",
                "a=rtpmap:8 PCMA/8000",
                "a=sendonly",
                "a=control:trackID=2",
            ]
        return "\r\n".join(lines) + "\r\n"

    def push(self, kind: str, pkt: bytes) -> None:
        """RTP packet from the VTO ("audio" or "video")."""
        track = TRACK_VIDEO if kind == "video" else TRACK_AUDIO
        keyframe = track == TRACK_VIDEO and self._sniff_params(pkt)
        out = self._tracks[track].rewrite(pkt)
        if track == TRACK_AUDIO and self._codecs[TRACK_AUDIO].pt == 8:
            self._push_audio_mixed(out)
            return
        if track == TRACK_VIDEO:
            if keyframe:
                self.gop = []
            if self.gop or keyframe:
                self.gop.append(out)
                if len(self.gop) > GOP_MAX_PACKETS:
                    self.gop = []  # no keyframe for too long: drop rather than grow
        for c in self._clients:
            if c.playing:
                c.send_rtp(track, out)

    def _push_audio_mixed(self, out: bytes) -> None:
        """Door audio to each viewer, with the other viewers' microphones mixed in."""
        players = [c for c in self._clients if c.playing]
        payload = _rtp_payload(out)
        n = len(payload)
        chunks = {}
        for c in self._clients:
            chunk = Speaker.take(c.speaker.to_peers, n)
            if chunk is not None:
                chunks[c] = chunk
        if not chunks or n == 0:
            for c in players:
                c.send_rtp(TRACK_AUDIO, out)
            return
        header = out[: len(out) - n]
        door = list(decode(payload, 8))
        for c in players:
            parts = [chunk for x, chunk in chunks.items() if x is not c]
            c.send_rtp(TRACK_AUDIO, header + encode_alaw(mix_into(door, parts)) if parts else out)

    def pull_door_frame(self, n: int = 160) -> bytes | None:
        """n samples of all viewers' microphones mixed, A-law, for the door station; None if nobody talks."""
        parts = []
        for c in self._clients:
            chunk = Speaker.take(c.speaker.to_door, n)
            if chunk is not None:
                parts.append(chunk)
        if not parts:
            return None
        return encode_alaw(mix_into([0] * n, parts))

    def _sniff_params(self, pkt: bytes) -> bool:
        """Remember SPS/PPS; True when the packet starts a keyframe (SPS or IDR start)."""
        payload = _rtp_payload(pkt)
        if not payload:
            return False
        nal_type = payload[0] & 0x1F
        if nal_type == 7:
            self._sps = payload
            return True
        if nal_type == 8:
            self._pps = payload
        elif nal_type == 24:  # STAP-A
            i, start = 1, False
            while i + 2 <= len(payload):
                size = struct.unpack_from("!H", payload, i)[0]
                nal = payload[i + 2 : i + 2 + size]
                if nal:
                    if nal[0] & 0x1F == 7:
                        self._sps = nal
                        start = True
                    elif nal[0] & 0x1F == 8:
                        self._pps = nal
                i += 2 + size
            return start
        elif nal_type == 28 and len(payload) > 1:  # FU-A
            return bool(payload[1] & 0x80) and payload[1] & 0x1F == 5 and not self.gop_has_sps()
        elif nal_type == 5:
            return not self.gop_has_sps()
        return False

    def gop_has_sps(self) -> bool:
        """True when the current GOP already started at an SPS (so its IDR belongs to it)."""
        if not self.gop:
            return False
        first = _rtp_payload(self.gop[0])
        return bool(first) and (first[0] & 0x1F in (7, 24))
