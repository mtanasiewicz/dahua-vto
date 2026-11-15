"""Dahua VTO intercom over SIP, with video and two-way audio in Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_ANSWER_ON_VIEW,
    CONF_LOCAL_IP,
    CONF_LOCAL_SIP_PORT,
    CONF_MONITOR_ON_VIEW,
    CONF_MONITOR_TARGET,
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

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.EVENT, Platform.SENSOR]

type IntercomConfigEntry = ConfigEntry[IntercomCore]


def _opt(entry: ConfigEntry, key: str, default):
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry) -> bool:
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
        answer_on_view=_opt(entry, CONF_ANSWER_ON_VIEW, True),
        monitor_on_view=_opt(entry, CONF_MONITOR_ON_VIEW, True),
    )
    try:
        await core.start()
    except OSError as err:
        await core.stop()
        raise ConfigEntryNotReady(f"Cannot open ports: {err}") from err
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
