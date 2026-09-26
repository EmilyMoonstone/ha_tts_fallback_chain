"""Tests for the fallback logic and the entities."""

from datetime import timedelta

import pytest

from homeassistant.components.tts import (
    async_get_media_source_audio,
    generate_media_source_id,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.tts_fallback_chain.const import DOMAIN, EVENT_STAGE_FAILED
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from .conftest import FakeTTS, quota_error

CHAIN = "tts.meine_kette"


async def setup_chain(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    options = {
        "language": "de",
        "timeout": 5,
        "error_cooldown": 5,
        "quota_cooldown": 60,
        "stages": [
            {"entity_id": "tts.free", "voice": "callirrhoe"},
            {"entity_id": "tts.paid", "voice": "callirrhoe"},
            {"entity_id": "tts.cloud", "voice": "KatjaNeural"},
        ],
    }
    options.update(overrides)
    entry = MockConfigEntry(domain=DOMAIN, title="Meine Kette", data={}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def speak(hass: HomeAssistant, message: str, language: str | None = None) -> bytes:
    media_id = generate_media_source_id(
        hass, message, engine=CHAIN, language=language, cache=False
    )
    _ext, data = await async_get_media_source_audio(hass, media_id)
    return data


async def test_entities_created(hass: HomeAssistant, fake_engines) -> None:
    await setup_chain(hass)
    assert hass.states.get(CHAIN) is not None
    assert hass.states.get("sensor.meine_kette_zuletzt_genutzter_tts_dienst") or hass.states.get(
        "sensor.meine_kette_last_used_tts_service"
    )
    tts_state = hass.states.get(CHAIN)
    assert tts_state.attributes["stages"] == ["tts.free", "tts.paid", "tts.cloud"]


async def test_first_stage_used(hass: HomeAssistant, fake_engines: dict[str, FakeTTS]) -> None:
    await setup_chain(hass)
    data = await speak(hass, "Hallo")
    assert data == b"Free:Hallo"
    call = fake_engines["free"].calls[0]
    assert call["options"]["voice"] == "callirrhoe"
    assert call["language"] == "de-DE"  # "de" matched to the engine's "de-DE"
    assert not fake_engines["paid"].calls
    assert not fake_engines["cloud"].calls


async def test_quota_error_falls_back_and_cools_down(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    entry = await setup_chain(hass)
    events = async_capture_events(hass, EVENT_STAGE_FAILED)
    fake_engines["free"].error = quota_error()

    assert await speak(hass, "Eins") == b"Paid:Eins"
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["quota_error"] is True
    assert events[0].data["entity_id"] == "tts.free"

    chain = entry.runtime_data
    stats = chain.stats[0]
    assert stats.cooldown_until is not None
    remaining = stats.cooldown_until - dt_util.utcnow()
    assert timedelta(minutes=59) < remaining <= timedelta(minutes=60)

    # During the cooldown the free stage is not asked at all.
    calls_before = len(fake_engines["free"].calls)
    assert await speak(hass, "Zwei") == b"Paid:Zwei"
    assert len(fake_engines["free"].calls) == calls_before

    # After the cooldown it is tried again.
    fake_engines["free"].error = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=61))
    stats.cooldown_until = dt_util.utcnow() - timedelta(seconds=1)
    assert await speak(hass, "Drei") == b"Free:Drei"
    assert stats.cooldown_until is None


async def test_regular_error_short_cooldown(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    entry = await setup_chain(hass)
    fake_engines["free"].error = HomeAssistantError("Connection reset")
    assert await speak(hass, "Hallo") == b"Paid:Hallo"
    remaining = entry.runtime_data.stats[0].cooldown_until - dt_util.utcnow()
    assert timedelta(minutes=4) < remaining <= timedelta(minutes=5)


async def test_all_google_down_uses_cloud(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    await setup_chain(hass)
    fake_engines["free"].error = quota_error()
    fake_engines["paid"].error = HomeAssistantError("API key invalid")
    assert await speak(hass, "Hallo") == b"Cloud:Hallo"
    assert fake_engines["cloud"].calls[0]["options"]["voice"] == "KatjaNeural"

    sensor = next(
        state
        for state in hass.states.async_all("sensor")
        if state.entity_id.startswith("sensor.meine_kette")
    )
    assert sensor.state == "Cloud"
    assert sensor.attributes["stage"] == 3
    assert sensor.attributes["stages"][0]["failures"] == 1
    assert sensor.attributes["stages"][0]["in_cooldown"] is True


async def test_timeout_falls_back(hass: HomeAssistant, fake_engines: dict[str, FakeTTS]) -> None:
    await setup_chain(hass, timeout=0.2)
    fake_engines["free"].delay = 2
    assert await speak(hass, "Hallo") == b"Paid:Hallo"


async def test_stages_in_cooldown_are_last_resort(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    entry = await setup_chain(hass)
    chain = entry.runtime_data
    for stats in chain.stats:
        stats.cooldown_until = dt_util.utcnow() + timedelta(hours=1)
    # Everything is cooling down -> still try in order instead of failing.
    assert await speak(hass, "Hallo") == b"Free:Hallo"


async def test_all_fail_raises(hass: HomeAssistant, fake_engines: dict[str, FakeTTS]) -> None:
    await setup_chain(hass)
    for engine in fake_engines.values():
        engine.error = HomeAssistantError("down")
    with pytest.raises(HomeAssistantError):
        await speak(hass, "Hallo")


async def test_missing_entity_is_skipped(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    await setup_chain(
        hass,
        stages=[{"entity_id": "tts.does_not_exist"}, {"entity_id": "tts.cloud"}],
    )
    assert await speak(hass, "Hallo") == b"Cloud:Hallo"
    # No voice configured -> engine default options, no voice key forced.
    assert "voice" not in fake_engines["cloud"].calls[0]["options"]


async def test_stage_language_and_extra_options(
    hass: HomeAssistant, fake_engines: dict[str, FakeTTS]
) -> None:
    await setup_chain(
        hass,
        stages=[
            {
                "entity_id": "tts.cloud",
                "voice": "KatjaNeural",
                "language": "de-CH",
                "extra_options": {"audio_output": "mp3"},
            }
        ],
    )
    assert await speak(hass, "Hallo", language="en-US") == b"Cloud:Hallo"
    call = fake_engines["cloud"].calls[0]
    assert call["language"] == "de-CH"
    assert call["options"]["audio_output"] == "mp3"


async def test_reset_button(hass: HomeAssistant, fake_engines: dict[str, FakeTTS]) -> None:
    entry = await setup_chain(hass)
    fake_engines["free"].error = quota_error()
    await speak(hass, "Hallo")
    assert entry.runtime_data.stats[0].cooldown_until is not None

    button = next(
        state.entity_id
        for state in hass.states.async_all("button")
        if state.entity_id.startswith("button.meine_kette")
    )
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert entry.runtime_data.stats[0].cooldown_until is None


async def test_unload(hass: HomeAssistant, fake_engines) -> None:
    entry = await setup_chain(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
