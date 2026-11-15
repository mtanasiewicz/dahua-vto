"""RTSP client that pulls video and door audio from the VTO.

The VTO's RTSP stream is available at any time (also while it rings and during a
call), so it is the source of the picture and the door audio. SIP is used only for
call control and for sending the microphone to the VTO.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import struct
import urllib.parse
from dataclasses import dataclass
from typing import Callable

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_S = 25
RECONNECT_S = (1, 2, 5, 10)


@dataclass
class UpstreamMedia:
    kind: str  # "video" or "audio"
    pt: int
    rtpmap: str  # e.g. "H264/90000"
    fmtp: str  # without the "a=fmtp:<pt> " prefix
    control: str

    @property
    def clock(self) -> int:
        try:
            return int(self.rtpmap.split("/")[1])
        except (IndexError, ValueError):
            return 90000 if self.kind == "video" else 8000


def parse_sdp_media(sdp: str) -> list[UpstreamMedia]:
    medias: list[UpstreamMedia] = []
    cur: dict | None = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            if cur:
                medias.append(_finish(cur))
            parts = line[2:].split()
            cur = {"kind": parts[0], "pts": [int(p) for p in parts[3:] if p.isdigit()], "rtpmap": {}, "fmtp": {}, "control": ""}
        elif cur is None:
            continue
        elif line.startswith("a=rtpmap:"):
            pt, val = line[9:].split(" ", 1)
            cur["rtpmap"][int(pt)] = val
        elif line.startswith("a=fmtp:"):
            pt, val = (line[7:].split(" ", 1) + [""])[:2]
            if pt.isdigit():
                cur["fmtp"][int(pt)] = val
        elif line.startswith("a=control:"):
            cur["control"] = line[10:]
    if cur:
        medias.append(_finish(cur))
    return [m for m in medias if m.kind in ("video", "audio") and m.pt >= 0]


def _finish(cur: dict) -> UpstreamMedia:
    pt = cur["pts"][0] if cur["pts"] else -1
    default = {0: "PCMU/8000", 8: "PCMA/8000"}.get(pt, "")
    return UpstreamMedia(cur["kind"], pt, cur["rtpmap"].get(pt, default), cur["fmtp"].get(pt, ""), cur["control"])


class VtoRtspClient:
    """Keeps one RTSP session to the VTO while `wanted` is set; reconnects on errors."""

    def __init__(
        self,
        url: str,
        on_medias: Callable[[list[UpstreamMedia]], None],
        on_packet: Callable[[str, bytes], None],
    ) -> None:
        self._url = url
        u = urllib.parse.urlsplit(url)
        self._user = urllib.parse.unquote(u.username or "")
        self._password = urllib.parse.unquote(u.password or "")
        self._host = u.hostname or ""
        self._port = u.port or 554
        netloc = self._host if not u.port else f"{self._host}:{u.port}"
        self._base = urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, ""))
        self._on_medias = on_medias
        self._on_packet = on_packet
        self._task: asyncio.Task | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0
        self._session = ""
        self._auth: tuple[str, dict[str, str]] | None = None
        self._channels: dict[int, str] = {}
        self.connected = False
        self.stats = {"video": 0, "audio": 0, "sessions": 0}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._close()

    # ----------------------------------------------------------------- loop

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("VTO RTSP: %s", err)
            finally:
                self._close()
            await asyncio.sleep(RECONNECT_S[min(attempt, len(RECONNECT_S) - 1)])
            attempt += 1

    async def _session_once(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), 5)
        self._cseq = 0
        self._session = ""
        self._channels = {}
        _, body = await self._request("DESCRIBE", self._base, {"Accept": "application/sdp"})
        medias = parse_sdp_media(body)
        _LOGGER.debug("VTO RTSP SDP:\n%s", body)
        if not medias:
            raise ConnectionError("no media in SDP")
        chosen = []
        for kind in ("video", "audio"):
            m = next((m for m in medias if m.kind == kind), None)
            if m is None:
                continue
            ch = len(chosen) * 2
            url = m.control if m.control.startswith("rtsp://") else self._base.rstrip("/") + "/" + m.control
            headers = {"Transport": f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1}"}
            hdrs, _ = await self._request("SETUP", url, headers)
            tr = hdrs.get("transport", "")
            mm = re.search(r"interleaved=(\d+)", tr)
            self._channels[int(mm.group(1)) if mm else ch] = kind
            chosen.append(m)
        self._on_medias(chosen)
        await self._request("PLAY", self._base, {"Range": "npt=0.000-"})
        self.connected = True
        self.stats["sessions"] += 1
        _LOGGER.info("VTO RTSP connected: %s", ", ".join(f"{m.kind} {m.rtpmap}" for m in chosen))
        loop = asyncio.get_running_loop()
        next_keepalive = loop.time() + KEEPALIVE_S
        while True:
            timeout = max(0.1, next_keepalive - loop.time())
            try:
                first = await asyncio.wait_for(self._reader.readexactly(1), timeout)
            except asyncio.TimeoutError:
                self._send("GET_PARAMETER", self._base, {})
                next_keepalive = loop.time() + KEEPALIVE_S
                continue
            if first == b"$":
                hdr = await asyncio.wait_for(self._reader.readexactly(3), 10)
                size = struct.unpack("!H", hdr[1:])[0]
                data = await asyncio.wait_for(self._reader.readexactly(size), 10)
                kind = self._channels.get(hdr[0])
                if kind and len(data) > 12:
                    self.stats[kind] += 1
                    self._on_packet(kind, data)
            else:
                # an RTSP response (keepalive) - read and drop it
                rest = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), 10)
                m = re.search(rb"content-length:\s*(\d+)", first + rest, re.I)
                if m:
                    await self._reader.readexactly(int(m.group(1)))

    # -------------------------------------------------------------- helpers

    def _close(self) -> None:
        self.connected = False
        if self._writer:
            self._writer.close()
        self._reader = self._writer = None

    def _send(self, method: str, url: str, headers: dict[str, str]) -> None:
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}", "User-Agent: HA-DahuaIntercom"]
        if self._session:
            lines.append(f"Session: {self._session}")
        if self._auth:
            lines.append(self._authorization(method, url))
        lines += [f"{k}: {v}" for k, v in headers.items()]
        assert self._writer is not None
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())

    async def _request(self, method: str, url: str, headers: dict[str, str]) -> tuple[dict[str, str], str]:
        for _ in range(2):
            self._send(method, url, headers)
            status, hdrs, body = await self._read_response()
            if status == 401 and self._user:
                self._auth = _parse_challenge(hdrs.get("www-authenticate", ""))
                continue
            if status != 200:
                raise ConnectionError(f"{method} -> {status}")
            if "session" in hdrs:
                self._session = hdrs["session"].split(";")[0].strip()
            return hdrs, body
        raise ConnectionError(f"{method} -> unauthorized")

    async def _read_response(self) -> tuple[int, dict[str, str], str]:
        assert self._reader is not None
        while True:
            first = await asyncio.wait_for(self._reader.readexactly(1), 10)
            if first == b"$":  # interleaved data before the reply
                hdr = await self._reader.readexactly(3)
                await self._reader.readexactly(struct.unpack("!H", hdr[1:])[0])
                continue
            head = (first + await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), 10)).decode(errors="replace")
            break
        lines = head.split("\r\n")
        status = int(lines[0].split()[1])
        hdrs: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                # keep the first Digest challenge if several WWW-Authenticate headers come
                if k == "www-authenticate" and k in hdrs and "digest" in hdrs[k].lower():
                    continue
                hdrs[k] = v.strip()
        body = ""
        n = int(hdrs.get("content-length", "0") or 0)
        if n:
            body = (await asyncio.wait_for(self._reader.readexactly(n), 10)).decode(errors="replace")
        return status, hdrs, body

    def _authorization(self, method: str, url: str) -> str:
        assert self._auth is not None
        scheme, p = self._auth
        if scheme == "basic":
            token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
            return f"Authorization: Basic {token}"
        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        ha1 = hashlib.md5(f"{self._user}:{realm}:{self._password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{url}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return f'Authorization: Digest username="{self._user}", realm="{realm}", nonce="{nonce}", uri="{url}", response="{resp}"'


def _parse_challenge(value: str) -> tuple[str, dict[str, str]]:
    scheme = "digest" if value.lower().startswith("digest") else "basic"
    return scheme, dict(re.findall(r'(\w+)="?([^",]*)"?', value))
