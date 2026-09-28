"""Sensor that shows which stage of the chain answered last."""

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FallbackChainConfigEntry
from .entity import FallbackChainEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FallbackChainConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensor."""
    async_add_entities([LastStageSensor(entry)])


class LastStageSensor(FallbackChainEntity, SensorEntity):
    """Name of the TTS engine that produced the last announcement."""

    _attr_translation_key = "last_stage"

    def __init__(self, entry: FallbackChainConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "last_stage")

    @property
    def native_value(self) -> str | None:
        """Return the name of the last successful stage."""
        if self.chain.last_stage_index is None:
            return "Alle fehlgeschlagen" if self.chain.last_error else None
        return self.chain.stage_name(self.chain.last_stage_index)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details for every stage."""
        index = self.chain.last_stage_index
        return {
            "stage": index + 1 if index is not None else None,
            "entity_id": self.chain.stages[index].entity_id if index is not None else None,
            "last_used": self.chain.last_used_at.isoformat() if self.chain.last_used_at else None,
            "last_duration": (
                round(self.chain.last_duration, 1)
                if self.chain.last_duration is not None
                else None
            ),
            "last_error": self.chain.last_error,
            "stages": self.chain.as_dict(),
        }
