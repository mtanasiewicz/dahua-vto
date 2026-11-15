"""Per-phone ring notification level (Zwykłe / Ważne / Krytyczne / Wyłączone)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import IntercomConfigEntry
from .entity import IntercomEntity
from .notifications import DEFAULT_LEVEL, LEVELS, Phone


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    core = entry.runtime_data
    async_add_entities([NotificationLevel(core, phone) for phone in core.notifier.phones])


class NotificationLevel(IntercomEntity, SelectEntity, RestoreEntity):
    _attr_options = LEVELS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bell-ring"

    def __init__(self, core, phone: Phone) -> None:
        super().__init__(core, f"notify_{phone.service}")
        self._phone = phone
        self._attr_translation_key = "notify_level"
        self._attr_translation_placeholders = {"phone": phone.name}
        self._attr_current_option = DEFAULT_LEVEL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in LEVELS:
            self._attr_current_option = last.state
        self.core.notifier.set_level(self._phone.service, self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.core.notifier.set_level(self._phone.service, option)
        self.async_write_ha_state()
