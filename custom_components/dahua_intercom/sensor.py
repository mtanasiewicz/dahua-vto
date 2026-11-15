"""Call state sensor."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .const import CALL_STATES
from .entity import IntercomEntity


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([CallStateSensor(entry.runtime_data, "call_state")])


class CallStateSensor(IntercomEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATES

    @property
    def native_value(self) -> str:
        return self.core.state

    @property
    def extra_state_attributes(self):
        return self.core.attributes()
