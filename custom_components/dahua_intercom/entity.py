"""Base entity."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .core import IntercomCore


class IntercomEntity(Entity):
    """Entity bound to one door station."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, core: IntercomCore, key: str) -> None:
        self.core = core
        self._attr_unique_id = f"{core.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, core.entry_id)},
            name="Domofon",
            manufacturer="Dahua",
            model="VTO (SIP)",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.core.entry_id), self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
