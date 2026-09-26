"""Shared entity base for the TTS Fallback Chain integration."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import FallbackChainConfigEntry
from .const import DOMAIN


class FallbackChainEntity(Entity):
    """Base entity that belongs to the device of a fallback chain."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: FallbackChainConfigEntry, key: str) -> None:
        """Initialize the entity."""
        self._entry = entry
        self.chain = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="TTS Fallback Chain",
            model="TTS-Fallback-Kette",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Refresh the state whenever the chain reports a change."""
        await super().async_added_to_hass()
        self.async_on_remove(self.chain.async_add_listener(self.async_write_ha_state))
