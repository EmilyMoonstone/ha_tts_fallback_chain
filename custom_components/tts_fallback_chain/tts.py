"""TTS entity that forwards to the first working engine of the chain."""

from typing import Any

from homeassistant.components.tts import TextToSpeechEntity, TtsAudioType
from homeassistant.const import CONF_LANGUAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FallbackChainConfigEntry
from .entity import FallbackChainEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FallbackChainConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the TTS entity."""
    async_add_entities([FallbackChainTTSEntity(hass, entry)])


class FallbackChainTTSEntity(FallbackChainEntity, TextToSpeechEntity):
    """A TTS engine that asks several TTS engines one after another."""

    # Home Assistant's TTS core requires engine.name to be set, so this entity
    # carries the chain name itself instead of inheriting the device name.
    _attr_has_entity_name = False
    _attr_supported_options: list[str] = []

    def __init__(self, hass: HomeAssistant, entry: FallbackChainConfigEntry) -> None:
        """Initialize the TTS entity."""
        super().__init__(entry, "tts")
        self._attr_name = entry.title
        self._default_language: str = (
            entry.options.get(CONF_LANGUAGE) or hass.config.language
        )

    @property
    def default_language(self) -> str:
        """Return the default language."""
        return self._default_language

    @property
    def supported_languages(self) -> list[str]:
        """Return all languages any of the stage engines supports."""
        languages = self.chain.supported_languages()
        languages.add(self._default_language)
        return sorted(languages)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return information about the last used stage."""
        index = self.chain.last_stage_index
        return {
            "last_stage": index + 1 if index is not None else None,
            "last_stage_entity_id": (
                self.chain.stages[index].entity_id if index is not None else None
            ),
            "stages": [stage.entity_id for stage in self.chain.stages],
        }

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        """Load TTS audio from the first stage that works."""
        return await self.chain.async_get_tts_audio(message, language)
