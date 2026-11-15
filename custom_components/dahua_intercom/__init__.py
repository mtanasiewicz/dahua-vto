"""Dahua VTO intercom over SIP, with video and two-way audio in Home Assistant."""
from __future__ import annotations

import logging
import os
import urllib.parse

from homeassistant.components import network
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_GO2RTC_API_PORT,
    CONF_LOCAL_IP,
    CONF_LOCAL_SIP_PORT,
    CONF_MAIN_STREAM,
    CONF_MONITOR_TARGET,
    CONF_PUBLIC_HOST,
    CONF_SOURCE_CAMERA,
    CONF_WEBRTC_PORT,
    DEFAULT_GO2RTC_API_PORT,
    DEFAULT_SOURCE_CAMERA,
    DEFAULT_WEBRTC_PORT,
    DOMAIN,
    CONF_REALM,
    CONF_RTP_PORT,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_SIP_PORT,
    DEFAULT_LOCAL_SIP_PORT,
    DEFAULT_MONITOR_TARGET,
    DEFAULT_REALM,
    DEFAULT_RTP_PORT,
    DEFAULT_RTSP_HOST,
    DEFAULT_RTSP_PORT,
    DEFAULT_SIP_PORT,
)
from .core import IntercomCore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CAMERA, Platform.EVENT, Platform.SENSOR]

CARD_URL = "/dahua_intercom/dahua-intercom-card.js"

type IntercomConfigEntry = ConfigEntry[IntercomCore]


def _opt(entry: ConfigEntry, key: str, default):
    return entry.options.get(key, entry.data.get(key, default))


async def _register_card(hass: HomeAssistant) -> None:
    """Serve the intercom card and load it on every dashboard (once per HA run)."""
    if hass.data.get(f"{DOMAIN}_card"):
        return
    hass.data[f"{DOMAIN}_card"] = True
    path = os.path.join(os.path.dirname(__file__), "frontend", "dahua-intercom-card.js")
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, path, cache_headers=False)])
    version = await hass.async_add_executor_job(lambda: int(os.path.getmtime(path)))
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")


def _default_public_host(hass: HomeAssistant) -> str:
    url = hass.config.external_url or ""
    return urllib.parse.urlsplit(url).hostname or ""


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry) -> bool:
    await _register_card(hass)
    host = entry.data[CONF_HOST]
    local_ip = _opt(entry, CONF_LOCAL_IP, "") or await network.async_get_source_ip(hass, target_ip=host)
    core = IntercomCore(
        hass,
        entry.entry_id,
        server=host,
        server_port=entry.data.get(CONF_SIP_PORT, DEFAULT_SIP_PORT),
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        realm=entry.data.get(CONF_REALM, DEFAULT_REALM),
        local_ip=local_ip,
        local_sip_port=_opt(entry, CONF_LOCAL_SIP_PORT, DEFAULT_LOCAL_SIP_PORT),
        rtp_port=_opt(entry, CONF_RTP_PORT, DEFAULT_RTP_PORT),
        rtsp_host=_opt(entry, CONF_RTSP_HOST, DEFAULT_RTSP_HOST),
        rtsp_port=_opt(entry, CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        monitor_target=_opt(entry, CONF_MONITOR_TARGET, DEFAULT_MONITOR_TARGET),
        source_camera=_opt(entry, CONF_SOURCE_CAMERA, DEFAULT_SOURCE_CAMERA),
        main_stream=_opt(entry, CONF_MAIN_STREAM, False),
        public_host=_opt(entry, CONF_PUBLIC_HOST, "") or _default_public_host(hass),
        webrtc_port=_opt(entry, CONF_WEBRTC_PORT, DEFAULT_WEBRTC_PORT),
        go2rtc_api_port=_opt(entry, CONF_GO2RTC_API_PORT, DEFAULT_GO2RTC_API_PORT),
        storage_dir=hass.config.path(DOMAIN),
    )
    try:
        await core.start()
    except (OSError, RuntimeError) as err:
        await core.stop()
        raise ConfigEntryNotReady(f"Cannot start: {err}") from err
    entry.runtime_data = core
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: IntercomConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.stop()
    return ok
