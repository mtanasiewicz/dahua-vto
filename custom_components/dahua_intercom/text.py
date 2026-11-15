"""Per-iPhone ring sound: the name of a sound file in the Companion app.

iOS plays only sounds inside the app (system sounds imported in Companion App ->
Notifications -> Sounds, or files put in the app's Sounds folder with the Files
app). Android has no such field: the sound belongs to the notification channel
and is chosen in the phone's settings.
"""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import IntercomConfigEntry
from .entity import IntercomEntity
from .notifications import DEFAULT_SOUND, Phone


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    core = entry.runtime_data
    async_add_entities([NotificationSound(core, p) for p in core.notifier.phones if p.os == "ios"])


class NotificationSound(IntercomEntity, TextEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:music-note"
    _attr_native_max = 100

    def __init__(self, core, phone: Phone) -> None:
        super().__init__(core, f"sound_{phone.service}")
        self._phone = phone
        self._attr_translation_key = "notify_sound"
        self._attr_translation_placeholders = {"phone": phone.name}
        self._attr_native_value = DEFAULT_SOUND

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "", "unknown", "unavailable"):
            self._attr_native_value = last.state
        self.core.notifier.set_sound(self._phone.service, self._attr_native_value)

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value.strip() or DEFAULT_SOUND
        self.core.notifier.set_sound(self._phone.service, self._attr_native_value)
        self.async_write_ha_state()
