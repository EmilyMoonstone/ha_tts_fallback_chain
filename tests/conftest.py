"""Fixtures for the TTS Fallback Chain tests."""

from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.components.tts import TextToSpeechEntity, TtsAudioType, Voice
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

FAKE_DOMAIN = "fake_tts"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture(autouse=True)
def tts_cache_dir(tmp_path) -> Generator[None]:
    """Keep the TTS cache out of the testing config dir."""
    with patch(
        "homeassistant.components.tts.DEFAULT_CACHE_DIR", str(tmp_path / "tts")
    ):
        yield


class FakeTTS(TextToSpeechEntity):
    """A controllable TTS engine."""

    _attr_should_poll = False

    def __init__(
        self,
        name: str,
        languages: list[str] | None = None,
        options: list[str] | None = None,
        voices: list[Voice] | None = None,
        default_language: str = "en-US",
    ) -> None:
        self._attr_name = name
        self._attr_unique_id = name
        self._attr_supported_languages = languages or ["de-DE", "en-US"]
        self._attr_default_language = default_language
        self._attr_supported_options = options if options is not None else ["voice"]
        self._voices = voices or []
        self.error: Exception | None = None
        self.delay: float = 0
        self.calls: list[dict[str, Any]] = []

    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        return self._voices or None

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any]
    ) -> TtsAudioType:
        import asyncio  # noqa: PLC0415

        self.calls.append({"message": message, "language": language, "options": options})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return "mp3", f"{self.name}:{message}".encode()


class FakeFlow(ConfigFlow):
    """Config flow of the fake integration."""


@pytest.fixture
async def fake_engines(hass: HomeAssistant) -> AsyncGenerator[dict[str, FakeTTS]]:
    """Set up three fake TTS entities: free, paid and cloud."""
    engines = {
        "free": FakeTTS(
            "Free",
            voices=[Voice("callirrhoe", "Callirrhoe (Easy-going)"), Voice("kore", "Kore")],
        ),
        "paid": FakeTTS("Paid", voices=[Voice("callirrhoe", "Callirrhoe (Easy-going)")]),
        "cloud": FakeTTS(
            "Cloud",
            languages=["de-DE", "de-CH", "en-US"],
            options=["voice", "audio_output"],
            voices=[Voice("KatjaNeural", "Katja"), Voice("ConradNeural", "Conrad")],
            default_language="en-US",
        ),
        "novoice": FakeTTS("NoVoice", options=[]),
    }

    async def async_setup_entry_platform(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        async_add_entities(list(engines.values()))

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, ["tts"])
        return True

    mock_integration(
        hass, MockModule(FAKE_DOMAIN, async_setup_entry=async_setup_entry)
    )
    mock_platform(hass, f"{FAKE_DOMAIN}.config_flow")
    mock_platform(
        hass,
        f"{FAKE_DOMAIN}.tts",
        MockPlatform(async_setup_entry=async_setup_entry_platform),
    )
    hass.config.language = "de"
    hass.config.country = "DE"
    assert await async_setup_component(hass, "tts", {})
    with mock_config_flow(FAKE_DOMAIN, FakeFlow):
        entry = MockConfigEntry(domain=FAKE_DOMAIN)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield engines


def quota_error() -> HomeAssistantError:
    """Return an error like the Gemini integration raises on a used up quota."""
    try:
        raise RuntimeError(
            "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota'}}"
        )
    except RuntimeError as err:
        return HomeAssistantError(str(err))
