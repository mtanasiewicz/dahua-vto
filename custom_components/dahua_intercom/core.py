"""Glue between the SIP agent, the media relay and the RTSP server."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
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
    VIEWER_GRACE_S,
)
from .media import MediaRelay
from .rtsp import RtspServer
from .sip import Call, SipAgent

_LOGGER = logging.getLogger(__name__)


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
        answer_on_view: bool,
        monitor_on_view: bool,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.monitor_target = monitor_target
        self.answer_on_view = answer_on_view
        self.monitor_on_view = monitor_on_view
        self.rtsp_url = f"rtsp://{rtsp_host}:{rtsp_port}/intercom"
        self.media = MediaRelay(rtp_port, rtp_port + 2, self._on_media)
        self.rtsp = RtspServer(rtsp_host, rtsp_port, self._on_viewers, self.media.push_mic)
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
        self.state = STATE_IDLE
        self.ringing = False
        self.caller: str | None = None
        self._viewer_call = False
        self._idle_task: asyncio.Task | None = None
        self._busy = asyncio.Lock()

    async def start(self) -> None:
        await self.media.start()
        await self.rtsp.start()
        await self.sip.start()

    async def stop(self) -> None:
        await self.sip.stop()
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
        return {
            "caller": self.caller,
            "viewers": self.viewers,
            "stream_url": self.rtsp_url,
            **self.media.stats,
        }

    # ------------------------------------------------------------ actions

    async def async_answer(self) -> bool:
        async with self._busy:
            ok = await self.sip.answer()
            if ok:
                self._viewer_call = self.viewers > 0
            return ok

    async def async_hangup(self) -> None:
        async with self._busy:
            await self.sip.hangup()
            self.media.end()
            self._refresh()

    async def async_live_view(self) -> bool:
        async with self._busy:
            if self.ringing:
                ok = await self.sip.answer()
            else:
                ok = await self.sip.call_out(self.monitor_target)
            if ok:
                self._viewer_call = self.viewers > 0
            return ok

    # ------------------------------------------------------------ events

    @callback
    def _on_media(self, kind: str, pkt: bytes) -> None:
        self.rtsp.push(kind, pkt)

    @callback
    def _on_viewers(self, count: int) -> None:
        _LOGGER.debug("RTSP viewers: %s", count)
        if count > 0:
            if self._idle_task:
                self._idle_task.cancel()
                self._idle_task = None
            if self.state in (STATE_TALKING, STATE_LIVE_VIEW, STATE_CALLING):
                self._viewer_call = True
            elif self.ringing and self.answer_on_view:
                self.hass.async_create_task(self.async_answer())
            elif self.monitor_on_view and self.state == STATE_IDLE:
                self.hass.async_create_task(self.async_live_view())
        elif self._viewer_call and self._idle_task is None:
            self._idle_task = self.hass.async_create_task(self._hangup_when_unwatched())
        self._refresh()

    async def _hangup_when_unwatched(self) -> None:
        try:
            await asyncio.sleep(VIEWER_GRACE_S)
            if self.viewers == 0 and self.state in (STATE_TALKING, STATE_LIVE_VIEW, STATE_CALLING):
                _LOGGER.debug("No viewers left, hanging up")
                await self.async_hangup()
        except asyncio.CancelledError:
            pass
        finally:
            self._idle_task = None

    @callback
    def _on_sip_event(self, event: str, call: Call | None) -> None:
        if event == "registration":
            self._refresh()
            return
        if event == "ringing" and call is not None:
            self.ringing = True
            self.caller = call.caller
            self.hass.bus.async_fire(EVENT_RING, {"caller": call.caller, "entry_id": self.entry_id})
            if self.state == STATE_IDLE:
                self.state = STATE_RINGING
            if self.viewers > 0 and self.answer_on_view:
                self.hass.async_create_task(self.async_answer())
        elif event == "calling":
            self.state = STATE_CALLING
        elif event == "active" and call is not None:
            if call.direction == "in":
                self.ringing = False
                self.state = STATE_TALKING
            else:
                self.state = STATE_LIVE_VIEW
            if call.plan:
                self.media.begin(call.plan)
            self.hass.bus.async_fire(EVENT_CALL, {"state": self.state, "caller": self.caller, "entry_id": self.entry_id})
        elif event == "ring_ended":
            self.ringing = False
        elif event.startswith("ended:"):
            reason = event.split(":", 1)[1]
            if call is not None and call.direction == "in" and call.state == "ended" and self.sip.call is None:
                self.ringing = False
            if self.sip.call is None or self.sip.call.state == "ended":
                self.media.end()
                self._viewer_call = False
                self.state = STATE_IDLE
            elif self.sip.call.state == "ringing":
                # live view ended but a ring is still pending
                self.media.end()
                self.state = STATE_RINGING
            if self.sip.pending_ring is None and (self.sip.call is None or self.sip.call.state != "ringing"):
                self.ringing = False
            if not self.ringing and self.state == STATE_IDLE:
                self.caller = None
            self.hass.bus.async_fire(EVENT_CALL, {"state": STATE_IDLE, "reason": reason, "entry_id": self.entry_id})
        self._refresh()

    @callback
    def _refresh(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry_id))
