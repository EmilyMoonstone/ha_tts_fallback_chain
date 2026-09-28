"""Config and options flow for the TTS Fallback Chain integration."""

from collections.abc import Mapping
from typing import Any

try:  # Home Assistant switched from voluptuous to its drop-in probatio
    import probatio as vol
except ImportError:  # pragma: no cover - older Home Assistant versions
    import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    LanguageSelector,
    LanguageSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import language as language_util

from .chain import get_tts_entity
from .const import (
    CONF_ENTITY_ID,
    CONF_ERROR_COOLDOWN,
    CONF_EXTRA_OPTIONS,
    CONF_LANGUAGE,
    CONF_MAX_WAIT,
    CONF_QUOTA_COOLDOWN,
    CONF_REDEFINE_STAGES,
    CONF_STAGES,
    CONF_TIMEOUT,
    CONF_VOICE,
    DEFAULT_ERROR_COOLDOWN,
    DEFAULT_MAX_WAIT,
    DEFAULT_NAME,
    DEFAULT_QUOTA_COOLDOWN,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

STEP_STAGE_ENTITY = "stage_entity"
STEP_STAGE_OPTIONS = "stage_options"
STEP_STAGES = "stages"
STEP_FINISH = "finish"


def _settings_schema(hass: HomeAssistant, defaults: Mapping[str, Any]) -> dict:
    """Return the schema fields for the general settings of a chain."""
    return {
        vol.Required(
            CONF_LANGUAGE, default=defaults.get(CONF_LANGUAGE) or hass.config.language
        ): LanguageSelector(LanguageSelectorConfig()),
        vol.Required(
            CONF_TIMEOUT, default=defaults.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        ): NumberSelector(
            NumberSelectorConfig(
                min=2, max=180, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_MAX_WAIT, default=defaults.get(CONF_MAX_WAIT, DEFAULT_MAX_WAIT)
        ): NumberSelector(
            NumberSelectorConfig(
                min=5, max=600, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_ERROR_COOLDOWN,
            default=defaults.get(CONF_ERROR_COOLDOWN, DEFAULT_ERROR_COOLDOWN),
        ): NumberSelector(
            NumberSelectorConfig(
                min=0, max=1440, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_QUOTA_COOLDOWN,
            default=defaults.get(CONF_QUOTA_COOLDOWN, DEFAULT_QUOTA_COOLDOWN),
        ): NumberSelector(
            NumberSelectorConfig(
                min=0, max=1440, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX
            )
        ),
    }


def _clean_settings(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the general settings entered by the user."""
    return {
        CONF_LANGUAGE: user_input[CONF_LANGUAGE],
        CONF_TIMEOUT: float(user_input[CONF_TIMEOUT]),
        CONF_MAX_WAIT: float(user_input[CONF_MAX_WAIT]),
        CONF_ERROR_COOLDOWN: float(user_input[CONF_ERROR_COOLDOWN]),
        CONF_QUOTA_COOLDOWN: float(user_input[CONF_QUOTA_COOLDOWN]),
    }


class _StageFlowMixin:
    """Steps to build the ordered list of stages, shared by both flows."""

    hass: HomeAssistant
    _settings: dict[str, Any]
    _stages: list[dict[str, Any]]
    _previous_stages: list[dict[str, Any]]
    _pending_entity_id: str | None = None

    async def async_step_stage_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the TTS entity of the next stage."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id: str = user_input[CONF_ENTITY_ID]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if any(stage[CONF_ENTITY_ID] == entity_id for stage in self._stages):
                errors[CONF_ENTITY_ID] = "duplicate_stage"
            elif registry_entry is not None and registry_entry.platform == DOMAIN:
                errors[CONF_ENTITY_ID] = "own_entity"
            else:
                self._pending_entity_id = entity_id
                return await self.async_step_stage_options()

        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=STEP_STAGE_ENTITY,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ENTITY_ID): EntitySelector(
                        EntitySelectorConfig(domain="tts")
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "stage": str(len(self._stages) + 1),
                "stages": self._stages_summary(),
            },
        )

    async def async_step_stage_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set voice, language and extra options of the chosen stage."""
        entity_id = self._pending_entity_id
        assert entity_id is not None
        entity = get_tts_entity(self.hass, entity_id)
        errors: dict[str, str] = {}

        if user_input is not None:
            voice = str(user_input.get(CONF_VOICE) or "").strip()
            language = str(user_input.get(CONF_LANGUAGE) or "").strip()
            extra = user_input.get(CONF_EXTRA_OPTIONS) or {}
            timeout = user_input.get(CONF_TIMEOUT)
            supported_options = (entity.supported_options or []) if entity else None

            if not isinstance(extra, Mapping):
                errors[CONF_EXTRA_OPTIONS] = "invalid_extra_options"
            elif (
                voice
                and supported_options is not None
                and CONF_VOICE not in supported_options
            ):
                errors[CONF_VOICE] = "voice_not_supported"
            elif (
                language
                and entity is not None
                and entity.supported_languages
                and language not in entity.supported_languages
            ):
                errors[CONF_LANGUAGE] = "language_not_supported"
            else:
                stage: dict[str, Any] = {CONF_ENTITY_ID: entity_id}
                if voice:
                    stage[CONF_VOICE] = voice
                if language:
                    stage[CONF_LANGUAGE] = language
                if extra:
                    stage[CONF_EXTRA_OPTIONS] = dict(extra)
                if timeout:
                    stage[CONF_TIMEOUT] = float(timeout)
                self._stages.append(stage)
                self._pending_entity_id = None
                return await self.async_step_stages()

        previous = next(
            (s for s in self._previous_stages if s[CONF_ENTITY_ID] == entity_id), {}
        )
        suggested = user_input if user_input is not None else previous

        schema: dict[Any, Any] = {}
        voice_options = self._voice_options(entity)
        schema[
            vol.Optional(
                CONF_VOICE, description={"suggested_value": suggested.get(CONF_VOICE)}
            )
        ] = (
            SelectSelector(
                SelectSelectorConfig(
                    options=voice_options,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                )
            )
            if voice_options
            else TextSelector()
        )
        language_selector: Any = TextSelector()
        if entity is not None and entity.supported_languages:
            language_selector = LanguageSelector(
                LanguageSelectorConfig(languages=list(entity.supported_languages))
            )
        schema[
            vol.Optional(
                CONF_LANGUAGE, description={"suggested_value": suggested.get(CONF_LANGUAGE)}
            )
        ] = language_selector
        schema[
            vol.Optional(
                CONF_TIMEOUT, description={"suggested_value": suggested.get(CONF_TIMEOUT)}
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=2, max=300, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX
            )
        )
        schema[
            vol.Optional(
                CONF_EXTRA_OPTIONS,
                description={"suggested_value": suggested.get(CONF_EXTRA_OPTIONS)},
            )
        ] = ObjectSelector()

        state = self.hass.states.get(entity_id)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=STEP_STAGE_OPTIONS,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "stage": str(len(self._stages) + 1),
                "entity": state.name if state else entity_id,
                "entity_id": entity_id,
                "options": ", ".join(entity.supported_options or []) if entity else "-",
                "default_timeout": f"{self._settings.get(CONF_TIMEOUT, DEFAULT_TIMEOUT):g}",
            },
        )

    async def async_step_stages(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the current chain and ask to add another stage or finish."""
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id=STEP_STAGES,
            menu_options=[STEP_STAGE_ENTITY, STEP_FINISH],
            description_placeholders={"stages": self._stages_summary()},
        )

    def _voice_options(self, entity: Any) -> list[SelectOptionDict]:
        """Return the voices the engine offers for the chain language."""
        if entity is None:
            return []
        language = self._settings.get(CONF_LANGUAGE) or self.hass.config.language
        supported = entity.supported_languages or []
        if supported and language not in supported:
            matches = language_util.matches(
                language, supported, country=self.hass.config.country
            )
            language = matches[0] if matches else entity.default_language
        try:
            voices = entity.async_get_supported_voices(language) or []
        except Exception:  # noqa: BLE001 - voices are only a convenience
            voices = []
        return [
            SelectOptionDict(value=voice.voice_id, label=f"{voice.name} ({voice.voice_id})")
            for voice in voices
        ]

    def _stages_summary(self) -> str:
        """Return the current stages as a markdown list."""
        if not self._stages:
            return "-"
        lines = []
        for index, stage in enumerate(self._stages, start=1):
            state = self.hass.states.get(stage[CONF_ENTITY_ID])
            name = state.name if state else stage[CONF_ENTITY_ID]
            details = [
                f"{key}: {stage[key]}"
                for key in (CONF_VOICE, CONF_LANGUAGE, CONF_TIMEOUT, CONF_EXTRA_OPTIONS)
                if stage.get(key)
            ]
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"{index}. **{name}** `{stage[CONF_ENTITY_ID]}`{suffix}")
        return "\n".join(lines)


class TtsFallbackChainConfigFlow(_StageFlowMixin, ConfigFlow, domain=DOMAIN):
    """Create a new fallback chain."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._title = DEFAULT_NAME
        self._settings = {}
        self._stages = []
        self._previous_stages = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the chain and choose the general settings."""
        if user_input is not None:
            self._title = user_input[CONF_NAME].strip() or DEFAULT_NAME
            self._settings = _clean_settings(user_input)
            return await self.async_step_stage_entity()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
                    **_settings_schema(self.hass, {}),
                }
            ),
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=self._title,
            data={},
            options={**self._settings, CONF_STAGES: self._stages},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return TtsFallbackChainOptionsFlow()


class TtsFallbackChainOptionsFlow(_StageFlowMixin, OptionsFlow):
    """Change the settings or the stages of an existing chain."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._settings = {}
        self._stages = []
        self._previous_stages = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the general settings, optionally rebuild the stages."""
        current = self.config_entry.options
        current_stages: list[dict[str, Any]] = list(current.get(CONF_STAGES, []))

        if user_input is not None:
            self._settings = _clean_settings(user_input)
            if user_input.get(CONF_REDEFINE_STAGES):
                self._previous_stages = current_stages
                self._stages = []
                return await self.async_step_stage_entity()
            return self.async_create_entry(
                data={**self._settings, CONF_STAGES: current_stages}
            )

        self._stages = current_stages
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    **_settings_schema(self.hass, current),
                    vol.Required(CONF_REDEFINE_STAGES, default=False): BooleanSelector(),
                }
            ),
            description_placeholders={"stages": self._stages_summary()},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save the options."""
        return self.async_create_entry(data={**self._settings, CONF_STAGES: self._stages})
