"""Glue between the SIP agent, the VTO RTSP pull, our RTSP server and go2rtc.

Media paths:
  picture + door audio:  VTO RTSP --(VtoRtspClient)--> RtspServer --> go2rtc --> WebRTC card
  microphone:            card --> go2rtc --> RtspServer backchannel --> MediaRelay --> SIP RTP --> VTO
Call control (ring, answer, hang up) is SIP; answering on any device makes the VTO
cancel the call on the others.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    EVENT_CALL,
    EVENT_RING,
    SIGNAL_UPDATE,
    STATE_CALLING,
    STATE_IDLE,
    STATE_LIVE_VIEW,
    STATE_RINGING,
    STATE_TALKING,
    UPSTREAM_IDLE_S,
)
from .go2rtc import Go2rtc, WebRtcSession
from .media import MediaRelay
from .rtsp import RtspServer
from .sip import Call, SipAgent
from .vto_rtsp import VtoRtspClient

_LOGGER = logging.getLogger(__name__)

_IN_CALL = (STATE_TALKING, STATE_LIVE_VIEW, STATE_CALLING)

# The VTO ends a call placed by a room station after ~2 minutes. For "Połącz" we
# dial again at once, so a longer talk only has a short audio gap.
REDIAL_MIN_CALL_S = 90  # a remote hang-up after at least this long is the VTO time limit
REDIAL_MAX_TOTAL_S = 15 * 60  # stop redialling after this long in total
REDIAL_DELAY_S = 0.3


class IntercomCore:
    """Runtime object for one door station."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        server: str,
        server_port: int,
        username: str,
        password: str,
        realm: str,
        local_ip: str,
        local_sip_port: int,
        rtp_port: int,
        rtsp_host: str,
        rtsp_port: int,
        monitor_target: str,
        source_camera: str,
        main_stream: bool,
        public_host: str,
        webrtc_port: int,
        go2rtc_api_port: int,
        storage_dir: str,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.monitor_target = monitor_target
        self.source_camera = source_camera
        self.main_stream = main_stream
        self.rtsp_url = f"rtsp://{rtsp_host}:{rtsp_port}/intercom"
        self.media = MediaRelay(rtp_port, rtp_port + 2, self._on_sip_media)
        self.rtsp = RtspServer(rtsp_host, rtsp_port, self._on_viewers, self.media.push_mic)
        self.http = async_get_clientsession(hass)
        self.go2rtc = Go2rtc(
            storage_dir,
            api_port=go2rtc_api_port,
            webrtc_port=webrtc_port,
            public_host=public_host,
            rtsp_url=self.rtsp_url,
            session=self.http,
        )
        self.sip = SipAgent(
            server=server,
            server_port=server_port,
            username=username,
            password=password,
            realm=realm,
            local_ip=local_ip,
            local_port=local_sip_port,
            audio_port=rtp_port,
            video_port=rtp_port + 2,
            on_event=self._on_sip_event,
        )
        self.upstream: VtoRtspClient | None = None
        self.state = STATE_IDLE
        self.ringing = False
        self.caller: str | None = None
        self.answered_at: float | None = None
        self._upstream_stop: asyncio.TimerHandle | None = None
        self._busy = asyncio.Lock()
        self._talk_wanted = False  # user pressed Połącz and has not ended the call
        self._talk_until = 0.0
        self._out_started: float | None = None
        self._redial_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.media.start()
        await self.rtsp.start()
        await self.sip.start()
        await self.go2rtc.start()

    async def stop(self) -> None:
        self._cancel_redial()
        await self.sip.stop()
        if self.upstream:
            await self.upstream.stop()
        await self.go2rtc.stop()
        await self.rtsp.stop()
        await self.media.stop()

    # --------------------------------------------------------- properties

    @property
    def registered(self) -> bool:
        return self.sip.registered

    @property
    def viewers(self) -> int:
        return self.rtsp.viewers

    def attributes(self) -> dict[str, Any]:
        up = self.upstream
        return {
            "caller": self.caller,
            "viewers": self.viewers,
            "stream_url": self.rtsp_url,
            "upstream_connected": bool(up and up.connected),
            "upstream_video": up.stats["video"] if up else 0,
            "upstream_audio": up.stats["audio"] if up else 0,
            **self.media.stats,
        }

    # ------------------------------------------------------------ actions

    async def async_answer(self) -> bool:
        self._cancel_redial()
        async with self._busy:
            if self.sip.call and self.sip.call.direction == "out":
                await self.sip.hangup()  # an old live view call must not block the answer
            ok = await self.sip.answer()
            _LOGGER.info("Answer %s", "ok" if ok else "ignored (nothing ringing)")
            return ok

    async def async_hangup(self) -> None:
        self._cancel_redial()
        async with self._busy:
            await self.sip.hangup()
            self.media.end()
            self._refresh()

    async def async_live_view(self) -> bool:
        """"Połącz": call the door station ourselves for a two-way talk (or answer a ring)."""
        if self.ringing:
            return await self.async_answer()
        async with self._busy:
            if self.sip.call is not None and self.sip.call.state in ("active", "calling"):
                return True  # already connected
            self._update_upstream(force=True)
            self._talk_wanted = True
            self._talk_until = self.hass.loop.time() + REDIAL_MAX_TOTAL_S
            ok = await self.sip.call_out(self.monitor_target)
            if not ok:
                self._talk_wanted = False
            _LOGGER.info("Outgoing call to %s %s", self.monitor_target, "ok" if ok else "failed")
            return ok

    async def async_snapshot(self) -> bytes | None:
        self._update_upstream(force=True)
        return await self.go2rtc.snapshot()

    def new_webrtc_session(self) -> WebRtcSession:
        self._update_upstream(force=True)
        return WebRtcSession(self.go2rtc, self.http)

    # ----------------------------------------------------------- upstream

    async def _ensure_upstream(self) -> None:
        if self.upstream is not None:
            self.upstream.start()
            return
        url = await self._source_url()
        if not url:
            _LOGGER.warning("No RTSP source from %s yet", self.source_camera)
            return
        if self.main_stream:
            url = url.replace("subtype=1", "subtype=0")
        self.upstream = VtoRtspClient(url, self.rtsp.set_upstream, self.rtsp.push)
        self.upstream.start()

    async def _source_url(self) -> str | None:
        from homeassistant.components.camera import async_get_stream_source  # noqa: PLC0415

        try:
            return await async_get_stream_source(self.hass, self.source_camera)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Stream source of %s unavailable: %s", self.source_camera, err)
            return None

    @callback
    def _update_upstream(self, force: bool = False) -> None:
        wanted = force or self.viewers > 0 or self.ringing or self.state in _IN_CALL
        loop = self.hass.loop
        if wanted:
            if self._upstream_stop:
                self._upstream_stop.cancel()
                self._upstream_stop = None
            if self.upstream is None or not self.upstream.running:
                self.hass.async_create_task(self._ensure_upstream())
            if force and self.viewers == 0 and not self.ringing and self.state not in _IN_CALL:
                # nobody may come: still stop later
                self._upstream_stop = loop.call_later(UPSTREAM_IDLE_S, self._stop_upstream)
        elif self.upstream and self.upstream.running and self._upstream_stop is None:
            self._upstream_stop = loop.call_later(UPSTREAM_IDLE_S, self._stop_upstream)

    @callback
    def _stop_upstream(self) -> None:
        self._upstream_stop = None
        if self.viewers > 0 or self.ringing or self.state in _IN_CALL:
            return
        if self.upstream:
            _LOGGER.debug("Stopping VTO RTSP (idle)")
            self.hass.async_create_task(self.upstream.stop())
            self.rtsp.reset_cache()

    # ------------------------------------------------------------ events

    @callback
    def _on_sip_media(self, kind: str, pkt: bytes) -> None:
        """RTP from the VTO over SIP: unused, the picture and audio come from RTSP."""

    @callback
    def _on_viewers(self, count: int) -> None:
        _LOGGER.debug("RTSP viewers: %s", count)
        self._update_upstream()
        self._refresh()

    @callback
    def _on_sip_event(self, event: str, call: Call | None) -> None:
        if event == "registration":
            self._refresh()
            return
        if event == "ringing" and call is not None:
            self.ringing = True
            self.caller = call.caller
            self.hass.bus.async_fire(EVENT_RING, {"caller": call.caller, "entry_id": self.entry_id})
            if self.state in (STATE_IDLE, STATE_LIVE_VIEW, STATE_CALLING):
                self.state = STATE_RINGING
        elif event == "calling":
            self.state = STATE_CALLING
        elif event == "active" and call is not None:
            if call.direction == "in":
                self.ringing = False
                self.state = STATE_TALKING
                self.answered_at = self.hass.loop.time()
            else:
                self.state = STATE_LIVE_VIEW
                self._out_started = self.hass.loop.time()
            if call.plan:
                self.media.begin(call.plan)
            self.hass.bus.async_fire(EVENT_CALL, {"state": self.state, "caller": self.caller, "entry_id": self.entry_id})
        elif event == "ring_ended":
            self.ringing = False
            if self.state == STATE_RINGING:
                self.state = STATE_IDLE
        elif event.startswith("ended:"):
            reason = event.split(":", 1)[1]
            if call is not None and self._should_redial(call, reason):
                # keep showing the call as active; the picture and the card stay as they are
                self.media.end()
                self._redial_task = self.hass.async_create_task(self._redial())
                self._refresh()
                return
            if call is not None and call.direction == "out":
                self._talk_wanted = False
            cur = self.sip.call
            if cur is None or cur.state == "ended":
                self.media.end()
                self.state = STATE_IDLE
                self.ringing = False
            elif cur.state == "ringing":
                self.media.end()
                self.state = STATE_RINGING
            if not self.ringing and self.state == STATE_IDLE:
                self.caller = None
                self.answered_at = None
            self.hass.bus.async_fire(EVENT_CALL, {"state": STATE_IDLE, "reason": reason, "entry_id": self.entry_id})
        self._update_upstream()
        self._refresh()

    def _should_redial(self, call: Call, reason: str) -> bool:
        if call.direction != "out" or not self._talk_wanted or reason != "remote_hangup":
            return False
        now = self.hass.loop.time()
        if self.ringing or self.sip.pending_ring is not None or now >= self._talk_until:
            return False
        # a quick remote hang-up is not the time limit (e.g. someone pressed the call button)
        return self._out_started is not None and now - self._out_started >= REDIAL_MIN_CALL_S

    async def _redial(self) -> None:
        try:
            await asyncio.sleep(REDIAL_DELAY_S)
            if not self._talk_wanted or self.ringing:
                raise asyncio.CancelledError
            _LOGGER.info("VTO ended the call (time limit), dialling again")
            async with self._busy:
                ok = await self.sip.call_out(self.monitor_target)
            if not ok:
                raise RuntimeError("redial failed")
        except (asyncio.CancelledError, RuntimeError) as err:
            if not isinstance(err, asyncio.CancelledError):
                _LOGGER.warning("Could not continue the call: %s", err)
            self._talk_wanted = False
            if self.sip.call is None or self.sip.call.state == "ended":
                self.state = STATE_RINGING if self.ringing else STATE_IDLE
                self.hass.bus.async_fire(EVENT_CALL, {"state": self.state, "reason": "redial_stopped", "entry_id": self.entry_id})
            self._update_upstream()
            self._refresh()
        finally:
            self._redial_task = None

    def _cancel_redial(self) -> None:
        self._talk_wanted = False
        if self._redial_task:
            self._redial_task.cancel()
            self._redial_task = None

    @callback
    def _refresh(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry_id))
