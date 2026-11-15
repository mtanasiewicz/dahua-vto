"""Binary sensors: ringing, SIP registration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .entity import IntercomEntity


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    core = entry.runtime_data
    async_add_entities([RingingSensor(core, "ringing"), RegisteredSensor(core, "registered")])


class RingingSensor(IntercomEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SOUND

    @property
    def is_on(self) -> bool:
        return self.core.ringing

    @property
    def extra_state_attributes(self):
        return {"caller": self.core.caller}


class RegisteredSensor(IntercomEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.core.registered

    @property
    def extra_state_attributes(self):
        return {"last_error": self.core.sip.last_error}
