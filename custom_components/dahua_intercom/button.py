"""Buttons: answer, hang up, live view."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntercomConfigEntry
from .entity import IntercomEntity


async def async_setup_entry(hass: HomeAssistant, entry: IntercomConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    core = entry.runtime_data
    async_add_entities(
        [AnswerButton(core, "answer"), HangupButton(core, "hangup"), LiveViewButton(core, "live_view"), OpenButton(core, "open")]
    )


class AnswerButton(IntercomEntity, ButtonEntity):
    _attr_icon = "mdi:phone"

    async def async_press(self) -> None:
        await self.core.async_answer()


class HangupButton(IntercomEntity, ButtonEntity):
    _attr_icon = "mdi:phone-hangup"

    async def async_press(self) -> None:
        await self.core.async_hangup()


class LiveViewButton(IntercomEntity, ButtonEntity):
    _attr_icon = "mdi:cctv"

    async def async_press(self) -> None:
        await self.core.async_live_view()


class OpenButton(IntercomEntity, ButtonEntity):
    _attr_icon = "mdi:door-open"

    async def async_press(self) -> None:
        await self.core.async_open()
