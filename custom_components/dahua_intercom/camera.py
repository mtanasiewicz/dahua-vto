"""Intercom camera: native WebRTC (video, door audio, microphone) through our go2rtc."""
from __future__ import annotations

import logging

from webrtc_models import RTCConfiguration, RTCIceCandidateInit, RTCIceServer

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
    WebRTCError,
    WebRTCSendMessage,
    async_get_image,
)
from homeassistant.components.camera.img_util import scale_jpeg_camera_image
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .entity import IntercomEntity
from .go2rtc import WebRtcSession

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([IntercomCamera(entry.runtime_data, "intercom")])


class IntercomCamera(IntercomEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, core, key: str) -> None:
        IntercomEntity.__init__(self, core, key)
        Camera.__init__(self)
        self._sessions: dict[str, WebRtcSession] = {}

    @property
    def extra_state_attributes(self):
        return {"call_state": self.core.state}

    async def stream_source(self) -> str | None:
        return self.core.rtsp_url

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Still picture for the idle card: from the source camera (cheap, no stream kept open)."""
        try:
            image = await async_get_image(self.hass, self.core.source_camera, timeout=8)
            if width and image.content_type == "image/jpeg":
                # the source camera ignores width: scale here so the idle card uses little data
                return await self.hass.async_add_executor_job(
                    scale_jpeg_camera_image, image, width, height or width * 9 // 16
                )
            return image.content
        except HomeAssistantError as err:
            _LOGGER.debug("Snapshot from %s failed (%s), using the stream", self.core.source_camera, err)
        return await self.core.async_snapshot()

    @callback
    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        return WebRTCClientConfiguration(
            configuration=RTCConfiguration(
                ice_servers=[RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"])]
            )
        )

    async def async_handle_async_webrtc_offer(self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage) -> None:
        session = self.core.new_webrtc_session()
        self._sessions[session_id] = session

        @callback
        def on_answer(sdp: str) -> None:
            send_message(WebRTCAnswer(answer=sdp))

        @callback
        def on_candidate(cand: str) -> None:
            send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(cand, sdp_m_line_index=0)))

        @callback
        def on_error(err: str) -> None:
            send_message(WebRTCError("webrtc_offer_failed", err))

        try:
            await session.start(offer_sdp, on_answer, on_candidate, on_error)
        except Exception as err:  # noqa: BLE001
            self._sessions.pop(session_id, None)
            _LOGGER.warning("WebRTC session failed: %s", err)
            send_message(WebRTCError("webrtc_offer_failed", str(err)))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: RTCIceCandidateInit) -> None:
        session = self._sessions.get(session_id)
        if session and candidate.candidate:
            await session.add_candidate(candidate.candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            self.hass.async_create_task(session.close())
