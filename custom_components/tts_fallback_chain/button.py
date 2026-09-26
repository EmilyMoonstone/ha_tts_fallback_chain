"""Button to clear all cooldowns of a chain."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FallbackChainConfigEntry
from .entity import FallbackChainEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FallbackChainConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([ResetCooldownsButton(entry)])


class ResetCooldownsButton(FallbackChainEntity, ButtonEntity):
    """Clears all cooldowns so the first stage is tried again immediately."""

    _attr_translation_key = "reset_cooldowns"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: FallbackChainConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "reset_cooldowns")

    async def async_press(self) -> None:
        """Reset all cooldowns."""
        self.chain.async_reset_cooldowns()
