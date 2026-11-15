"""Ring notifications to the Companion app, sent by the integration itself.

Why not an automation: the notification must leave the moment the SIP ring
arrives, with delivery settings that wake a sleeping phone at once
(Android: priority high + ttl 0; iOS: interruption level), and it must be
withdrawn from every phone as soon as anybody answers (phone or indoor panel).

Each phone has a level (select entity):
  off        - no notification
  normal     - a regular notification (Focus / Do Not Disturb may hold it back)
  important  - breaks through Focus (iOS time-sensitive; Android own channel,
               high priority - allow "override Do Not Disturb" for the channel)
  critical   - rings even when the phone is silenced (iOS critical alert;
               Android alarm stream)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

LEVELS = ["off", "normal", "important", "critical"]
DEFAULT_LEVEL = "important"
DEFAULT_SOUND = "default"  # iOS: a file in the Companion app's Library/Sounds, or "default"
TAG = "dahua_intercom_ring"
ACTION_OPEN = "DAHUA_INTERCOM_OPEN"
ACTION_IGNORE = "DAHUA_INTERCOM_IGNORE"
SEND_TIMEOUT_S = 10


@dataclass
class Phone:
    service: str  # notify service name, e.g. mobile_app_pixel_9_pro
    name: str
    os: str  # "ios" or "android"


def discover_phones(hass: HomeAssistant) -> list[Phone]:
    """All phones registered with the Companion app."""
    phones = []
    for entry in hass.config_entries.async_entries("mobile_app"):
        name = entry.data.get("device_name") or entry.title
        os_name = (entry.data.get("os_name") or "").lower()
        # iOS, iPadOS and the macOS app all use Apple push (same payload); the rest is Android
        apple = any(x in os_name for x in ("ios", "macos", "mac os"))
        phones.append(Phone(f"mobile_app_{slugify(name)}", name, "ios" if apple else "android"))
    return phones


LIVE_PARAM = "intercom=live"  # tells the card it was opened from the ring: go live at once


def live_url(url: str) -> str:
    """The notify URL with the card's go-live flag (before any #fragment)."""
    if not url:
        return url
    base, sep, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}{LIVE_PARAM}{sep}{frag}"


def _ring_payload(phone: Phone, level: str, *, url: str, image: str, sound: str = DEFAULT_SOUND) -> dict:
    url = live_url(url)
    data: dict = {
        "tag": TAG,
        "image": image,
        "actions": [
            {"action": ACTION_OPEN, "title": "Otwórz"},
            {"action": ACTION_IGNORE, "title": "Ignoruj"},
        ],
    }
    if url:
        data["url"] = url  # tapping the notification opens this page (otherwise the app's home)
    if phone.os == "ios":
        if level == "critical":
            data["push"] = {
                "interruption-level": "critical",
                "sound": {"name": sound, "critical": 1, "volume": 1.0},
            }
        else:
            data["push"] = {
                "interruption-level": "time-sensitive" if level == "important" else "active",
                "sound": sound,
            }
    else:
        data.update(
            {
                "priority": "high",  # FCM high priority: delivered at once, also in Doze
                "ttl": 0,
                "importance": "high",
                # Android fixes a channel's importance when the channel is first created and
                # the app can never raise it later; a phone that once made "Domofon" quiet
                # (only a dot on the icon, no pop-up) gets a fresh, high-importance channel.
                "channel": {
                    "critical": "alarm_stream",
                    "important": "ha-doorbell",
                    "normal": "ha-doorbell-normal",
                }[level],
                "visibility": "public",
            }
        )
        if url:
            data["clickAction"] = url
    return {"title": "Domofon", "message": "Ktoś dzwoni do furtki", "data": data}


def _open_app_payload(url: str) -> dict:
    """Android: bring the Companion app up on the intercom view (needs "Display over other apps")."""
    data: dict = {"priority": "high", "ttl": 0}
    if url:
        data["command"] = url  # without it the app opens on its home page
    return {"message": "command_webview", "data": data}


def _clear_payload(phone: Phone) -> dict:
    data: dict = {"tag": TAG}
    if phone.os == "android":
        data.update({"priority": "high", "ttl": 0})
    return {"message": "clear_notification", "data": data}


class Notifier:
    def __init__(self, hass: HomeAssistant, *, url: str, image_entity: str) -> None:
        self.hass = hass
        self.url = url
        self.image = f"/api/camera_proxy/{image_entity}"
        self.phones: list[Phone] = discover_phones(hass)
        self.levels: dict[str, str] = {p.service: DEFAULT_LEVEL for p in self.phones}
        self.sounds: dict[str, str] = {}
        self.auto_open: dict[str, bool] = {}
        self._sent: set[str] = set()

    def set_level(self, service: str, level: str) -> None:
        if level in LEVELS:
            self.levels[service] = level

    def set_sound(self, service: str, sound: str) -> None:
        self.sounds[service] = sound.strip() or DEFAULT_SOUND

    def set_auto_open(self, service: str, on: bool) -> None:
        self.auto_open[service] = on

    async def ring(self) -> None:
        targets = [p for p in self.phones if self.levels.get(p.service, DEFAULT_LEVEL) != "off"]
        self._sent = {p.service for p in targets}
        await asyncio.gather(
            *(
                self._send(
                    p,
                    _ring_payload(
                        p,
                        self.levels[p.service],
                        url=self.url,
                        image=self.image,
                        sound=self.sounds.get(p.service, DEFAULT_SOUND),
                    ),
                )
                for p in targets
            ),
            *(
                self._send(p, _open_app_payload(self.url))
                for p in targets
                if p.os == "android" and self.auto_open.get(p.service)
            ),
        )

    async def clear(self) -> None:
        targets = [p for p in self.phones if p.service in self._sent]
        self._sent = set()
        await asyncio.gather(*(self._send(p, _clear_payload(p)) for p in targets))

    async def _send(self, phone: Phone, payload: dict) -> None:
        if not self.hass.services.has_service("notify", phone.service):
            _LOGGER.warning("Notify service notify.%s not found", phone.service)
            return
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await self.hass.services.async_call("notify", phone.service, payload, blocking=True)
            _LOGGER.debug("Notification to %s: %s", phone.name, payload.get("message"))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Notification to %s failed: %s", phone.name, err)
