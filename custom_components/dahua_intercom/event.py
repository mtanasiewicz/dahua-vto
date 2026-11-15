"""Doorbell event entity (fires on every ring)."""
from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .const import EVENT_RING
from .entity import IntercomEntity


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DoorbellEvent(entry.runtime_data, "doorbell")])


class DoorbellEvent(IntercomEntity, EventEntity):
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_RING, self._on_ring))

    @callback
    def _on_ring(self, event: Event) -> None:
        if event.data.get("entry_id") != self.core.entry_id:
            return
        self._trigger_event("ring", {"caller": event.data.get("caller")})
        self.async_write_ha_state()

    @callback
    def _handle_update(self) -> None:
        """Event entities only change when they fire."""
