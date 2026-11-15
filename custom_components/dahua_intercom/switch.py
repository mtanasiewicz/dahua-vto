"""Per-Android-phone switch: open the Companion app on the intercom view when it rings.

Uses the Companion command_webview notification. The first time, Android shows the
"Display over other apps" permission screen; without it the app cannot open itself.
iOS does not allow an app to open itself, so iPhones get no such switch.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import IntercomConfigEntry
from .entity import IntercomEntity
from .notifications import Phone


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    core = entry.runtime_data
    async_add_entities([AutoOpen(core, p) for p in core.notifier.phones if p.os == "android"])


class AutoOpen(IntercomEntity, SwitchEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cellphone-arrow-down"

    def __init__(self, core, phone: Phone) -> None:
        super().__init__(core, f"auto_open_{phone.service}")
        self._phone = phone
        self._attr_translation_key = "auto_open"
        self._attr_translation_placeholders = {"phone": phone.name}
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
        self.core.notifier.set_auto_open(self._phone.service, self._attr_is_on)

    async def _set(self, on: bool) -> None:
        self._attr_is_on = on
        self.core.notifier.set_auto_open(self._phone.service, on)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
