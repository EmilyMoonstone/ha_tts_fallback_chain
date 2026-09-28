"""Tests for the config and options flow."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tts_fallback_chain.const import DOMAIN


async def test_full_flow(hass: HomeAssistant, fake_engines) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "TTS Fallback-Kette",
            "language": "de",
            "timeout": 20,
            "error_cooldown": 5,
            "quota_cooldown": 60,
        },
    )
    assert result["step_id"] == "stage_entity"

    # Stage 1: Google free with Callirrhoe
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.free"}
    )
    assert result["step_id"] == "stage_options"
    voice_selector = result["data_schema"].schema["voice"]
    values = [opt["value"] for opt in voice_selector.config["options"]]
    assert "callirrhoe" in values
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"voice": "callirrhoe"}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "stages"

    # Stage 2: paid
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "stage_entity"}
    )
    # duplicate is rejected
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.free"}
    )
    assert result["errors"] == {"entity_id": "duplicate_stage"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.paid"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"voice": "callirrhoe", "timeout": 45}
    )

    # Stage 3: cloud with Katja (voices of the German language are offered)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "stage_entity"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.cloud"}
    )
    values = [opt["value"] for opt in result["data_schema"].schema["voice"].config["options"]]
    assert "KatjaNeural" in values
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"voice": "KatjaNeural"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TTS Fallback-Kette"
    assert result["options"]["stages"] == [
        {"entity_id": "tts.free", "voice": "callirrhoe"},
        {"entity_id": "tts.paid", "voice": "callirrhoe", "timeout": 45.0},
        {"entity_id": "tts.cloud", "voice": "KatjaNeural"},
    ]
    assert result["options"]["max_wait"] == 120
    await hass.async_block_till_done()
    assert hass.states.get("tts.tts_fallback_kette") is not None


async def test_voice_not_supported_and_own_entity(hass: HomeAssistant, fake_engines) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Kette", "language": "de", "timeout": 20, "error_cooldown": 5, "quota_cooldown": 60},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.novoice"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"voice": "foo"}
    )
    assert result["errors"] == {"voice": "voice_not_supported"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # A second chain may not use the first chain as a stage.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Kette 2", "language": "de", "timeout": 20, "error_cooldown": 5, "quota_cooldown": 60},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity_id": "tts.kette"}
    )
    assert result["errors"] == {"entity_id": "own_entity"}


async def test_options_flow(hass: HomeAssistant, fake_engines) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kette",
        data={},
        options={
            "language": "de",
            "timeout": 20,
            "error_cooldown": 5,
            "quota_cooldown": 60,
            "stages": [
                {"entity_id": "tts.free", "voice": "callirrhoe"},
                {"entity_id": "tts.cloud", "voice": "KatjaNeural"},
            ],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Only change the settings
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"language": "de", "timeout": 10, "error_cooldown": 1, "quota_cooldown": 120, "redefine_stages": False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["quota_cooldown"] == 120
    assert len(entry.options["stages"]) == 2
    assert entry.runtime_data.timeout == 10

    # Rebuild the stages in a new order; previous voice is suggested
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"language": "de", "timeout": 10, "error_cooldown": 1, "quota_cooldown": 120, "redefine_stages": True},
    )
    assert result["step_id"] == "stage_entity"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "tts.cloud"}
    )
    voice_key = next(k for k in result["data_schema"].schema if k == "voice")
    assert voice_key.description == {"suggested_value": "KatjaNeural"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"voice": "KatjaNeural"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["stages"] == [{"entity_id": "tts.cloud", "voice": "KatjaNeural"}]
