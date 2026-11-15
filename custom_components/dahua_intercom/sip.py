"""Minimal SIP user agent for Dahua VTO door stations.

Only what a Dahua VTO needs: REGISTER with digest auth, incoming INVITE
(ringing / answer / CANCEL / BYE), outgoing INVITE (live view of the door
station) and BYE. Plain asyncio, UDP only, no third-party dependencies.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import random
import re
import time
from typing import Callable

_LOGGER = logging.getLogger(__name__)

T1 = 0.5
USER_AGENT = "HA-DahuaIntercom/1.0"


def _rand(n: int = 10) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


@dataclass
class SipMessage:
    """Parsed SIP request or response."""

    first: str
    headers: list[tuple[str, str]]
    body: str
    addr: tuple[str, int]

    @property
    def is_response(self) -> bool:
        return self.first.startswith("SIP/2.0")

    @property
    def code(self) -> int:
        return int(self.first.split()[1])

    @property
    def method(self) -> str:
        if self.is_response:
            return self.cseq_method
        return self.first.split()[0]

    def get(self, name: str, default: str = "") -> str:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if k == name]

    @property
    def call_id(self) -> str:
        return self.get("call-id")

    @property
    def cseq_num(self) -> int:
        return int(self.get("cseq", "0 X").split()[0])

    @property
    def cseq_method(self) -> str:
        return self.get("cseq", "0 X").split()[1]

    @property
    def branch(self) -> str:
        m = re.search(r"branch=([^;,\s]+)", self.get("via"))
        return m.group(1) if m else ""


_COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length", "c": "content-type"}


def parse_message(data: bytes, addr: tuple[str, int]) -> SipMessage | None:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    if not lines or not lines[0]:
        return None
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        k = _COMPACT.get(k, k)
        if k == "via":
            for part in v.split(","):
                headers.append((k, part.strip()))
        else:
            headers.append((k, v.strip()))
    return SipMessage(lines[0], headers, body, addr)


def tag_of(value: str) -> str:
    m = re.search(r";\s*tag=([^;>\s]+)", value)
    return m.group(1) if m else ""


def uri_of(value: str) -> str:
    m = re.search(r"<([^>]+)>", value)
    if m:
        return m.group(1)
    return value.split(";")[0].strip()


# ---------------------------------------------------------------- SDP


@dataclass
class SdpMedia:
    port: int
    payloads: list[int]
    rtpmap: dict[int, str] = field(default_factory=dict)
    fmtp: dict[int, str] = field(default_factory=dict)
    ip: str | None = None


@dataclass
class Sdp:
    ip: str | None
    media: dict[str, SdpMedia]


def parse_sdp(text: str) -> Sdp:
    ip = None
    media: dict[str, SdpMedia] = {}
    cur: SdpMedia | None = None
    for line in re.split(r"\r?\n", text):
        if line.startswith("c=IN IP4 "):
            addr = line.split()[-1].split("/")[0]
            if cur is None:
                ip = addr
            else:
                cur.ip = addr
        elif line.startswith("m="):
            parts = line[2:].split()
            try:
                pts = [int(p) for p in parts[3:]]
                cur = SdpMedia(port=int(parts[1]), payloads=pts)
            except ValueError:
                cur = None
                continue
            if parts[0] not in media:
                media[parts[0]] = cur
        elif cur is not None and line.startswith("a=rtpmap:"):
            pt, _, enc = line[9:].partition(" ")
            if pt.isdigit():
                cur.rtpmap[int(pt)] = enc.strip()
        elif cur is not None and line.startswith("a=fmtp:"):
            pt, _, val = line[7:].partition(" ")
            if pt.isdigit():
                cur.fmtp[int(pt)] = val.strip()
    return Sdp(ip, media)


@dataclass
class MediaPlan:
    """Negotiated media parameters for one call."""

    remote_ip: str
    audio_port: int | None
    audio_pt: int
    dtmf_pt: int | None
    video_port: int | None
    video_pt: int | None


def _find_pt(m: SdpMedia, name: str) -> int | None:
    for pt in m.payloads:
        enc = m.rtpmap.get(pt)
        if enc is None:
            # static payload types
            if name == "PCMA" and pt == 8:
                return pt
            if name == "PCMU" and pt == 0:
                return pt
            continue
        if enc.upper().startswith(name.upper() + "/"):
            if name in ("PCMA", "PCMU") and not enc.upper().endswith("/8000") and "/8000/" not in enc.upper():
                continue
            return pt
    return None


def plan_from_sdp(sdp: Sdp) -> MediaPlan | None:
    """Pick PCMA audio and H.264 video from a remote offer or answer."""
    audio = sdp.media.get("audio")
    video = sdp.media.get("video")
    if audio is None or audio.port == 0:
        return None
    apt = _find_pt(audio, "PCMA")
    if apt is None:
        return None
    dtmf = _find_pt(audio, "telephone-event")
    vpt = None
    vport = None
    if video is not None and video.port:
        vpt = _find_pt(video, "H264")
        if vpt is not None:
            vport = video.port
    ip = audio.ip or sdp.ip
    if not ip:
        return None
    return MediaPlan(ip, audio.port, apt, dtmf, vport, vpt)


def build_sdp(local_ip: str, audio_port: int, video_port: int, plan: MediaPlan | None) -> str:
    """Build our SDP (offer when plan is None, answer otherwise)."""
    sid = str(int(time.time()))
    apt = plan.audio_pt if plan else 8
    dtmf = plan.dtmf_pt if plan else 101
    vpt = (plan.video_pt if plan else 96)
    lines = [
        "v=0",
        f"o=- {sid} {sid} IN IP4 {local_ip}",
        "s=HA Dahua Intercom",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
    ]
    fmts = f"{apt}" + (f" {dtmf}" if dtmf is not None else "")
    lines += [f"m=audio {audio_port} RTP/AVP {fmts}", f"a=rtpmap:{apt} PCMA/8000"]
    if dtmf is not None:
        lines += [f"a=rtpmap:{dtmf} telephone-event/8000", f"a=fmtp:{dtmf} 0-15"]
    lines += ["a=ptime:20", "a=sendrecv"]
    if vpt is not None:
        lines += [
            f"m=video {video_port} RTP/AVP {vpt}",
            f"a=rtpmap:{vpt} H264/90000",
            f"a=fmtp:{vpt} profile-level-id=42001f;packetization-mode=1",
            "a=sendrecv",
        ]
    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------ the agent


@dataclass
class Call:
    """One SIP dialog (incoming ring or outgoing live view)."""

    direction: str  # "in" or "out"
    call_id: str
    local_tag: str
    remote_tag: str = ""
    local_uri: str = ""
    remote_uri: str = ""
    remote_target: str = ""
    remote_addr: tuple[str, int] | None = None
    local_cseq: int = 1
    invite: SipMessage | None = None  # incoming INVITE
    invite_cseq: int = 0  # outgoing INVITE cseq
    invite_branch: str = ""
    state: str = "init"  # ringing, calling, active, ended
    plan: MediaPlan | None = None
    last_response: bytes | None = None
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    route: list[str] = field(default_factory=list)  # Route set (Record-Route)
    caller: str = ""


class SipAgent(asyncio.DatagramProtocol):
    """SIP UA bound to one UDP port, registered to the VTO's SIP server."""

    def __init__(
        self,
        *,
        server: str,
        server_port: int,
        username: str,
        password: str,
        realm: str,
        local_ip: str,
        local_port: int,
        audio_port: int,
        video_port: int,
        on_event: Callable[[str, Call | None], None],
    ) -> None:
        self.server = server
        self.server_port = server_port
        self.username = username
        self.password = password
        self.domain = realm or server
        self.local_ip = local_ip
        self.local_port = local_port
        self.audio_port = audio_port
        self.video_port = video_port
        self._on_event = on_event
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reg_task: asyncio.Task | None = None
        self._reg_call_id = _rand(16) + "@" + local_ip
        self._reg_tag = _rand(8)
        self._reg_cseq = 0
        self.registered = False
        self.last_error: str | None = None
        self.call: Call | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    # ---------------------------------------------------------- plumbing

    @property
    def user_uri(self) -> str:
        return f"sip:{self.username.replace('#', '%23')}@{self.domain}"

    @property
    def contact(self) -> str:
        return f"<sip:{self.username.replace('#', '%23')}@{self.local_ip}:{self.local_port}>"

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.local_port))
        self._reg_task = loop.create_task(self._register_loop())

    async def stop(self) -> None:
        self._closing = True
        if self.call and self.call.state in ("active", "calling", "ringing"):
            try:
                await asyncio.wait_for(self.hangup(), 2)
            except Exception:  # noqa: BLE001
                pass
        if self._reg_task:
            self._reg_task.cancel()
        if self.registered:
            try:
                await asyncio.wait_for(self._register(expires=0), 2)
            except Exception:  # noqa: BLE001
                pass
        for t in list(self._tasks):
            t.cancel()
        if self._transport:
            self._transport.close()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self._transport = transport

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _send(self, data: str | bytes, addr: tuple[str, int] | None = None) -> None:
        if self._transport is None:
            return
        if isinstance(data, str):
            data = data.encode()
        _LOGGER.debug("SIP TX %s -> %s", data.split(b"\r\n", 1)[0].decode(errors="replace"), addr)
        self._transport.sendto(data, addr or (self.server, self.server_port))

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if len(data) < 8 or data in (b"\r\n\r\n", b"\r\n"):
            return
        msg = parse_message(data, addr)
        if msg is None:
            return
        _LOGGER.debug("SIP RX %s <- %s", msg.first, addr)
        if msg.is_response:
            fut = self._pending.get(msg.branch + msg.cseq_method)
            if fut is not None and not fut.done():
                if msg.code >= 200:
                    fut.set_result(msg)
                else:
                    self._on_provisional(msg)
            elif msg.cseq_method == "INVITE" and msg.code == 200:
                # retransmitted 200 OK for our INVITE: ACK again
                self._ack_outgoing(msg)
            return
        try:
            self._handle_request(msg)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error handling SIP request %s", msg.first)

    # ------------------------------------------------- client transactions

    def _request(
        self,
        method: str,
        uri: str,
        *,
        to: str,
        from_tag: str,
        call_id: str,
        cseq: int,
        to_tag: str = "",
        extra: str = "",
        body: str = "",
        branch: str | None = None,
        from_uri: str | None = None,
        route: list[str] | None = None,
    ) -> tuple[str, str]:
        branch = branch or "z9hG4bK" + _rand(12)
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport",
            *(f"Route: {r}" for r in route or []),
            "Max-Forwards: 70",
            f"From: <{from_uri or self.user_uri}>;tag={from_tag}",
            f"To: <{to}>" + (f";tag={to_tag}" if to_tag else ""),
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} {method}",
            f"Contact: {self.contact}",
            f"User-Agent: {USER_AGENT}",
        ]
        msg = "\r\n".join(lines) + "\r\n" + extra
        if body:
            msg += "Content-Type: application/sdp\r\n"
        msg += f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
        return msg, branch

    async def _transaction(self, build: Callable[[str], tuple[str, str]], method: str, uri: str, timeout: float = 8.0) -> SipMessage:
        """Send a request (re-sent with auth on 401/407) and wait for a final response."""
        auth = ""
        for attempt in range(2):
            msg, branch = build(auth)
            key = branch + method
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[key] = fut
            try:
                resp = await self._send_until(fut, msg, method, timeout)
            finally:
                self._pending.pop(key, None)
            if resp.code in (401, 407) and attempt == 0:
                hdr = resp.get("www-authenticate") if resp.code == 401 else resp.get("proxy-authenticate")
                auth = self._auth_header(hdr, method, uri, resp.code)
                if method == "INVITE":
                    self._ack_failure(resp, uri)
                continue
            return resp
        return resp

    async def _send_until(self, fut: asyncio.Future, msg: str, method: str, timeout: float) -> SipMessage:
        interval = T1
        deadline = time.monotonic() + timeout
        self._send(msg)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"SIP {method} timeout")
            try:
                return await asyncio.wait_for(asyncio.shield(fut), min(interval, remaining))
            except asyncio.TimeoutError:
                if fut.done():
                    return fut.result()
                # retransmit only until something provisional arrives (INVITE)
                if method != "INVITE" or not getattr(fut, "_provisional", False):
                    self._send(msg)
                interval = min(interval * 2, 4)

    def _on_provisional(self, msg: SipMessage) -> None:
        fut = self._pending.get(msg.branch + msg.cseq_method)
        if fut is not None:
            fut._provisional = True  # type: ignore[attr-defined]  # noqa: SLF001
        if msg.cseq_method == "INVITE" and self.call and self.call.direction == "out" and msg.code == 180:
            _LOGGER.debug("Live view call ringing")

    def _auth_header(self, challenge: str, method: str, uri: str, code: int) -> str:
        params = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
        realm = params.get("realm", self.domain)
        nonce = params.get("nonce", "")
        ha1 = _md5(f"{self.username}:{realm}:{self.password}")
        ha2 = _md5(f"{method}:{uri}")
        extra = ""
        if "qop" in params and "auth" in params["qop"].split(","):
            nc, cnonce = "00000001", _rand(8)
            response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
            extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        else:
            response = _md5(f"{ha1}:{nonce}:{ha2}")
        if "opaque" in params:
            extra += f', opaque="{params["opaque"]}"'
        name = "Authorization" if code == 401 else "Proxy-Authorization"
        return (
            f'{name}: Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm=MD5{extra}\r\n'
        )

    # ------------------------------------------------------- registration

    async def _register(self, expires: int) -> int:
        uri = f"sip:{self.domain}"

        def build(auth: str) -> tuple[str, str]:
            self._reg_cseq += 1
            return self._request(
                "REGISTER",
                uri,
                to=self.user_uri,
                from_tag=self._reg_tag,
                call_id=self._reg_call_id,
                cseq=self._reg_cseq,
                extra=auth + f"Expires: {expires}\r\n",
            )

        resp = await self._transaction(build, "REGISTER", uri)
        if resp.code != 200:
            raise ConnectionError(f"REGISTER rejected: {resp.first}")
        granted = expires
        m = re.search(r"expires=(\d+)", resp.get("contact"))
        if m:
            granted = int(m.group(1))
        elif resp.get("expires").isdigit():
            granted = int(resp.get("expires"))
        return granted

    async def _register_loop(self) -> None:
        while not self._closing:
            try:
                granted = await self._register(expires=300)
                if not self.registered:
                    _LOGGER.info("Registered to %s as %s (expires %ss)", self.server, self.username, granted)
                self.registered = True
                self.last_error = None
                self._on_event("registration", None)
                await asyncio.sleep(max(20, granted * 0.8))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self.registered or self.last_error != str(err):
                    _LOGGER.warning("SIP registration to %s failed: %s", self.server, err)
                self.registered = False
                self.last_error = str(err)
                self._on_event("registration", None)
                await asyncio.sleep(30)

    async def wait_registered(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.registered:
                return True
            if self.last_error and "rejected" in self.last_error:
                return False
            await asyncio.sleep(0.2)
        return self.registered

    # ------------------------------------------------ server transactions

    def _response(self, req: SipMessage, code: int, reason: str, *, to_tag: str = "", body: str = "", extra: str = "") -> bytes:
        to = req.get("to")
        if to_tag and not tag_of(to):
            to += f";tag={to_tag}"
        via_lines = "".join(f"Via: {v}\r\n" for v in self._via_with_received(req))
        if code > 100 and req.method == "INVITE":
            # RFC 3261 12.1.1: a UAS copies Record-Route into dialog-creating responses.
            # The VTO's built-in SIP server proxies the call and drops our 200 OK without it.
            via_lines += "".join(f"Record-Route: {r}\r\n" for r in req.get_all("record-route"))
        msg = (
            f"SIP/2.0 {code} {reason}\r\n{via_lines}"
            f"From: {req.get('from')}\r\nTo: {to}\r\nCall-ID: {req.call_id}\r\n"
            f"CSeq: {req.get('cseq')}\r\nContact: {self.contact}\r\nUser-Agent: {USER_AGENT}\r\n{extra}"
        )
        if body:
            msg += "Content-Type: application/sdp\r\n"
        msg += f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
        data = msg.encode()
        self._send(data, req.addr)
        return data

    @staticmethod
    def _via_with_received(req: SipMessage) -> list[str]:
        vias = req.get_all("via")
        if not vias:
            return []
        top = vias[0]
        host, port = req.addr
        if "rport" in top and "rport=" not in top:
            top = top.replace("rport", f"rport={port}", 1)
        if "received=" not in top:
            top += f";received={host}"
        return [top, *vias[1:]]

    def _handle_request(self, req: SipMessage) -> None:
        method = req.method
        call = self.call
        if self.pending_ring is not None and self.pending_ring.call_id == req.call_id:
            call = self.pending_ring
        same_call = call is not None and call.call_id == req.call_id
        if method == "INVITE":
            if same_call and call.direction == "in" and call.invite and req.cseq_num == call.invite.cseq_num:
                if call.last_response:  # retransmission
                    self._send(call.last_response, req.addr)
                return
            if same_call:  # re-INVITE inside our dialog: accept with our current SDP
                body = build_sdp(self.local_ip, self.audio_port, self.video_port, call.plan)
                self._response(req, 200, "OK", to_tag=call.local_tag, body=body)
                return
            self._incoming_invite(req)
        elif method == "ACK":
            if same_call:
                call.acked.set()
        elif method == "CANCEL":
            self._response(req, 200, "OK")
            if same_call and call.direction == "in" and call.state == "ringing" and call.invite:
                call.last_response = self._response(call.invite, 487, "Request Terminated", to_tag=call.local_tag)
                self._end_call(call, "cancelled")
        elif method == "BYE":
            self._response(req, 200, "OK")
            if same_call:
                self._end_call(call, "remote_hangup")
        elif method in ("OPTIONS", "INFO", "MESSAGE", "NOTIFY", "UPDATE"):
            self._response(req, 200, "OK")
        else:
            self._response(req, 405, "Method Not Allowed")

    def _incoming_invite(self, req: SipMessage) -> None:
        sdp = parse_sdp(req.body)
        plan = plan_from_sdp(sdp)
        if plan is None:
            self._response(req, 488, "Not Acceptable Here", to_tag=_rand(8))
            return
        busy = self.call is not None and self.call.state in ("ringing", "active", "calling")
        if busy and not (self.call.direction == "out"):
            self._response(req, 486, "Busy Here", to_tag=_rand(8))
            return
        local_tag = _rand(8)
        self._response(req, 100, "Trying")
        call = Call(
            direction="in",
            call_id=req.call_id,
            local_tag=local_tag,
            remote_tag=tag_of(req.get("from")),
            local_uri=uri_of(req.get("to")),
            remote_uri=uri_of(req.get("from")),
            remote_target=uri_of(req.get("contact")) or uri_of(req.get("from")),
            remote_addr=req.addr,
            invite=req,
            state="ringing",
            plan=plan,
            route=req.get_all("record-route"),
        )
        m = re.match(r"sips?:([^@;>]+)", call.remote_uri)
        call.caller = m.group(1) if m else call.remote_uri
        call.last_response = self._response(req, 180, "Ringing", to_tag=local_tag)
        if busy and self.call is not None:
            # A ring while we watch the live view: keep the view, remember the ring.
            self.pending_ring = call
        else:
            self.call = call
        _LOGGER.info("Incoming call from %s", call.caller)
        self._on_event("ringing", call)

    pending_ring: Call | None = None

    # ------------------------------------------------------- call control

    async def answer(self) -> bool:
        """Answer the ringing incoming call."""
        if self.pending_ring is not None:
            ring = self.pending_ring
            self.pending_ring = None
            if self.call and self.call.direction == "out":
                await self.hangup()
            if ring.state != "ringing":
                return False
            self.call = ring
        call = self.call
        if call is None or call.direction != "in" or call.state != "ringing" or call.invite is None:
            return False
        body = build_sdp(self.local_ip, self.audio_port, self.video_port, call.plan)
        call.last_response = self._response(call.invite, 200, "OK", to_tag=call.local_tag, body=body)
        call.state = "active"
        self._on_event("active", call)
        self._spawn(self._retransmit_ok(call))
        return True

    async def _retransmit_ok(self, call: Call) -> None:
        interval = T1
        waited = 0.0
        while waited < 32 and call.state == "active":
            try:
                await asyncio.wait_for(call.acked.wait(), interval)
                return
            except asyncio.TimeoutError:
                waited += interval
                if call.last_response and call.invite:
                    self._send(call.last_response, call.invite.addr)
                interval = min(interval * 2, 4)
        if call.state == "active" and not call.acked.is_set():
            _LOGGER.warning("No ACK for answered call, hanging up")
            await self.hangup()

    async def call_out(self, target: str) -> bool:
        """Call the door station (live view with two-way audio)."""
        if self.call is not None and self.call.state in ("ringing", "active", "calling"):
            return False
        uri = f"sip:{target}@{self.server}:{self.server_port}"
        call = Call(
            direction="out",
            call_id=_rand(16) + "@" + self.local_ip,
            local_tag=_rand(8),
            local_uri=self.user_uri,
            remote_uri=f"sip:{target}@{self.server}",
            remote_target=uri,
            remote_addr=(self.server, self.server_port),
            state="calling",
        )
        self.call = call
        self._on_event("calling", call)
        body = build_sdp(self.local_ip, self.audio_port, self.video_port, None)

        def build(auth: str) -> tuple[str, str]:
            if auth:
                call.local_cseq += 1
            msg, branch = self._request(
                "INVITE", uri, to=call.remote_uri, from_tag=call.local_tag, call_id=call.call_id,
                cseq=call.local_cseq, extra=auth, body=body,
            )
            call.invite_cseq = call.local_cseq
            call.invite_branch = branch
            return msg, branch

        try:
            resp = await self._transaction(build, "INVITE", uri, timeout=30)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Live view call failed: %s", err)
            self._end_call(call, "failed")
            return False
        if call.state != "calling":  # hung up meanwhile
            if resp.code == 200:
                call.remote_tag = tag_of(resp.get("to"))
                call.remote_target = uri_of(resp.get("contact")) or uri
                self._ack_outgoing(resp)
                await self._bye(call)
            return False
        if resp.code != 200:
            _LOGGER.warning("Live view call rejected: %s", resp.first)
            self._end_call(call, "rejected")
            return False
        call.remote_tag = tag_of(resp.get("to"))
        call.remote_target = uri_of(resp.get("contact")) or uri
        self._ack_outgoing(resp)
        plan = plan_from_sdp(parse_sdp(resp.body))
        if plan is None:
            await self._bye(call)
            self._end_call(call, "no_media")
            return False
        call.plan = plan
        call.state = "active"
        self._on_event("active", call)
        return True

    def _ack_outgoing(self, resp: SipMessage) -> None:
        call = self.call
        if call is None or call.call_id != resp.call_id:
            return
        target = uri_of(resp.get("contact")) or call.remote_target
        msg, _ = self._request(
            "ACK", target, to=call.remote_uri, from_tag=call.local_tag, call_id=call.call_id,
            cseq=resp.cseq_num, to_tag=tag_of(resp.get("to")),
        )
        self._send(msg)

    def _ack_failure(self, resp: SipMessage, uri: str) -> None:
        """ACK a non-2xx final response to INVITE (same branch, RFC 3261 17.1.1.3)."""
        m = re.search(r"branch=([^;,\s]+)", resp.get("via"))
        branch = m.group(1) if m else None
        msg, _ = self._request(
            "ACK", uri, to=uri_of(resp.get("to")), from_tag=tag_of(resp.get("from")), call_id=resp.call_id,
            cseq=resp.cseq_num, to_tag=tag_of(resp.get("to")), branch=branch,
        )
        self._send(msg)

    async def hangup(self) -> None:
        call = self.call
        if self.pending_ring is not None and (call is None or call.direction == "out"):
            # ignoring a ring we only remember: nothing to send, VTO keeps ringing others
            pass
        if call is None:
            return
        if call.state == "ringing" and call.direction == "in" and call.invite:
            call.last_response = self._response(call.invite, 486, "Busy Here", to_tag=call.local_tag)
            self._end_call(call, "rejected")
            return
        if call.state == "calling":
            self._end_call(call, "cancelled")
            msg, _ = self._request(
                "CANCEL", call.remote_target, to=call.remote_uri, from_tag=call.local_tag, call_id=call.call_id,
                cseq=call.invite_cseq, branch=call.invite_branch,
            )
            self._send(msg)
            return
        if call.state == "active":
            self._end_call(call, "local_hangup")
            await self._bye(call)

    async def _bye(self, call: Call) -> None:
        call.local_cseq = max(call.local_cseq, call.invite_cseq) + 1
        if call.direction == "in":
            from_uri, to_uri = call.local_uri, call.remote_uri
        else:
            from_uri, to_uri = call.local_uri or self.user_uri, call.remote_uri
        target = call.remote_target

        def build(auth: str) -> tuple[str, str]:
            if auth:
                call.local_cseq += 1
            return self._request(
                "BYE", target, to=to_uri, from_tag=call.local_tag, call_id=call.call_id,
                cseq=call.local_cseq, to_tag=call.remote_tag, extra=auth, from_uri=from_uri,
                route=call.route,
            )

        try:
            await self._transaction(build, "BYE", target, timeout=4)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("BYE not confirmed: %s", err)

    def _end_call(self, call: Call, reason: str) -> None:
        if call.state == "ended":
            return
        call.state = "ended"
        if self.pending_ring is call:
            self.pending_ring = None
            self._on_event("ring_ended", call)
            return
        if self.call is call:
            self.call = None
            if self.pending_ring is not None and self.pending_ring.state == "ringing":
                self.call = self.pending_ring
                self.pending_ring = None
        _LOGGER.info("Call ended (%s)", reason)
        self._on_event("ended:" + reason, call)
