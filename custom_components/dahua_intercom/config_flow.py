"""Config flow for Dahua Intercom."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_LOCAL_IP,
    CONF_LOCAL_SIP_PORT,
    CONF_LOCK,
    CONF_MAIN_STREAM,
    CONF_NOTIFY_URL,
    DEFAULT_LOCK,
    DEFAULT_NOTIFY_URL,
    CONF_MONITOR_TARGET,
    CONF_PUBLIC_HOST,
    CONF_SOURCE_CAMERA,
    CONF_WEBRTC_PORT,
    DEFAULT_SOURCE_CAMERA,
    DEFAULT_WEBRTC_PORT,
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
    DEFAULT_USERNAME,
    DOMAIN,
)
from .sip import SipAgent

_LOGGER = logging.getLogger(__name__)


async def _test_registration(hass, data: dict[str, Any]) -> str | None:
    """Try to register once. Returns an error key or None."""
    local_ip = await network.async_get_source_ip(hass, target_ip=data[CONF_HOST])
    agent = SipAgent(
        server=data[CONF_HOST],
        server_port=data[CONF_SIP_PORT],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        realm=data[CONF_REALM],
        local_ip=local_ip,
        local_port=DEFAULT_LOCAL_SIP_PORT + 100,
        audio_port=0,
        video_port=0,
        on_event=lambda *_: None,
    )
    try:
        await agent.start()
    except OSError:
        return "port_in_use"
    try:
        ok = await agent.wait_registered(8)
        if ok:
            return None
        if agent.last_error and "rejected" in agent.last_error:
            return "invalid_auth"
        return "cannot_connect"
    finally:
        await agent.stop()


class DahuaIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(f"{user_input[CONF_HOST]}_{user_input[CONF_USERNAME]}")
            self._abort_if_unique_id_configured()
            err = await _test_registration(self.hass, user_input)
            if err is None:
                return self.async_create_entry(title=f"Domofon {user_input[CONF_HOST]}", data=user_input)
            errors["base"] = err
        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(CONF_SIP_PORT, default=defaults.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)): int,
                vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_REALM, default=defaults.get(CONF_REALM, DEFAULT_REALM)): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            await self.hass.config_entries.async_unload(entry.entry_id)
            err = await _test_registration(self.hass, data)
            if err is None:
                return self.async_update_reload_and_abort(entry, data=data)
            await self.hass.config_entries.async_setup(entry.entry_id)
            errors["base"] = err
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(CONF_SIP_PORT, default=entry.data.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)): int,
                vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_REALM, default=entry.data.get(CONF_REALM, DEFAULT_REALM)): str,
            }
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DahuaIntercomOptionsFlow()


class DahuaIntercomOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_CAMERA, default=o.get(CONF_SOURCE_CAMERA, DEFAULT_SOURCE_CAMERA)): EntitySelector(
                    EntitySelectorConfig(domain="camera")
                ),
                vol.Required(CONF_MAIN_STREAM, default=o.get(CONF_MAIN_STREAM, False)): bool,
                vol.Required(CONF_LOCK, default=o.get(CONF_LOCK, DEFAULT_LOCK)): EntitySelector(
                    EntitySelectorConfig(domain=["lock", "button", "script", "switch"])
                ),
                vol.Required(CONF_NOTIFY_URL, default=o.get(CONF_NOTIFY_URL, DEFAULT_NOTIFY_URL)): str,
                vol.Optional(CONF_PUBLIC_HOST, default=o.get(CONF_PUBLIC_HOST, "")): str,
                vol.Required(CONF_WEBRTC_PORT, default=o.get(CONF_WEBRTC_PORT, DEFAULT_WEBRTC_PORT)): int,
                vol.Required(CONF_MONITOR_TARGET, default=o.get(CONF_MONITOR_TARGET, DEFAULT_MONITOR_TARGET)): str,
                vol.Required(CONF_LOCAL_SIP_PORT, default=o.get(CONF_LOCAL_SIP_PORT, DEFAULT_LOCAL_SIP_PORT)): int,
                vol.Required(CONF_RTP_PORT, default=o.get(CONF_RTP_PORT, DEFAULT_RTP_PORT)): int,
                vol.Required(CONF_RTSP_HOST, default=o.get(CONF_RTSP_HOST, DEFAULT_RTSP_HOST)): str,
                vol.Required(CONF_RTSP_PORT, default=o.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): int,
                vol.Optional(CONF_LOCAL_IP, default=o.get(CONF_LOCAL_IP, "")): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
