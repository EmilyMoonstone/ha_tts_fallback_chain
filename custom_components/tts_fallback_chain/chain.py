"""Fallback chain logic: ask the configured TTS engines one after another."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.tts import (
    async_get_media_source_audio,
    generate_media_source_id,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util, language as language_util

from .const import (
    CONF_ENTITY_ID,
    CONF_ERROR_COOLDOWN,
    CONF_EXTRA_OPTIONS,
    CONF_LANGUAGE,
    CONF_QUOTA_COOLDOWN,
    CONF_STAGES,
    CONF_TIMEOUT,
    CONF_VOICE,
    DEFAULT_ERROR_COOLDOWN,
    DEFAULT_QUOTA_COOLDOWN,
    DEFAULT_TIMEOUT,
    EVENT_ALL_STAGES_FAILED,
    EVENT_STAGE_FAILED,
    QUOTA_ERROR_MARKERS,
)

if TYPE_CHECKING:
    from homeassistant.components.tts import TextToSpeechEntity

_LOGGER = logging.getLogger(__name__)


def get_tts_entity(hass: HomeAssistant, entity_id: str) -> "TextToSpeechEntity | None":
    """Return the loaded TTS entity object for an entity ID, if available."""
    try:
        from homeassistant.components.tts.const import DATA_COMPONENT  # noqa: PLC0415
    except ImportError:  # pragma: no cover - very old Home Assistant
        return None
    if (component := hass.data.get(DATA_COMPONENT)) is None:
        return None
    return component.get_entity(entity_id)


def is_quota_error(err: BaseException) -> bool:
    """Return True if the error looks like an exhausted quota / rate limit."""
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__} {current}".lower()
        if any(marker in text for marker in QUOTA_ERROR_MARKERS):
            return True
        if getattr(current, "code", None) == 429 or getattr(current, "status", None) == 429:
            return True
        current = current.__cause__ or current.__context__
    return False


def _error_text(err: BaseException) -> str:
    """Return a short, readable description of an error."""
    if isinstance(err, TimeoutError):
        return "Timeout"
    text = str(err) or type(err).__name__
    return text if len(text) <= 300 else f"{text[:297]}..."


@dataclass(frozen=True, slots=True)
class Stage:
    """One configured stage of the chain."""

    entity_id: str
    voice: str | None = None
    language: str | None = None
    extra_options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Stage":
        """Create a stage from stored config entry options."""
        return cls(
            entity_id=data[CONF_ENTITY_ID],
            voice=data.get(CONF_VOICE) or None,
            language=data.get(CONF_LANGUAGE) or None,
            extra_options=dict(data.get(CONF_EXTRA_OPTIONS) or {}),
        )

    def build_options(self) -> dict[str, Any]:
        """Return the TTS options that are sent to the engine of this stage."""
        options = dict(self.extra_options)
        if self.voice:
            options[CONF_VOICE] = self.voice
        return options


@dataclass(slots=True)
class StageStats:
    """Runtime statistics of a stage."""

    successes: int = 0
    failures: int = 0
    cooldown_until: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None

    def in_cooldown(self, now: datetime) -> bool:
        """Return True if the stage should currently be skipped."""
        return self.cooldown_until is not None and self.cooldown_until > now


class FallbackChain:
    """Holds the configuration and runtime state of one fallback chain."""

    def __init__(
        self,
        hass: HomeAssistant,
        stages: list[Stage],
        timeout: float,
        error_cooldown: timedelta,
        quota_cooldown: timedelta,
    ) -> None:
        """Initialize the chain."""
        self.hass = hass
        self.stages = stages
        self.timeout = timeout
        self.error_cooldown = error_cooldown
        self.quota_cooldown = quota_cooldown
        self.stats = [StageStats() for _ in stages]
        self.last_stage_index: int | None = None
        self.last_used_at: datetime | None = None
        self.last_error: str | None = None
        self._listeners: list[Callable[[], None]] = []

    @classmethod
    def from_options(
        cls, hass: HomeAssistant, options: Mapping[str, Any]
    ) -> "FallbackChain":
        """Create the chain from config entry options."""
        return cls(
            hass,
            stages=[Stage.from_dict(stage) for stage in options.get(CONF_STAGES, [])],
            timeout=float(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
            error_cooldown=timedelta(
                minutes=float(options.get(CONF_ERROR_COOLDOWN, DEFAULT_ERROR_COOLDOWN))
            ),
            quota_cooldown=timedelta(
                minutes=float(options.get(CONF_QUOTA_COOLDOWN, DEFAULT_QUOTA_COOLDOWN))
            ),
        )

    # ------------------------------------------------------------------
    # Listeners (used by the sensor / TTS entity to refresh their state)

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback that is called when the state changes."""
        self._listeners.append(update_callback)

        @callback
        def remove_listener() -> None:
            self._listeners.remove(update_callback)

        return remove_listener

    @callback
    def _async_notify(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    # ------------------------------------------------------------------
    # Helpers

    def stage_name(self, index: int) -> str:
        """Return a human readable name of a stage."""
        entity_id = self.stages[index].entity_id
        if (state := self.hass.states.get(entity_id)) is not None:
            return state.name
        return entity_id

    @callback
    def async_reset_cooldowns(self) -> None:
        """Clear all cooldowns so every stage is tried again."""
        for stats in self.stats:
            stats.cooldown_until = None
        self._async_notify()

    def supported_languages(self) -> set[str]:
        """Return the union of the languages of all loaded stage engines."""
        languages: set[str] = set()
        for stage in self.stages:
            if stage.language:
                languages.add(stage.language)
            if (entity := get_tts_entity(self.hass, stage.entity_id)) is not None:
                languages.update(entity.supported_languages or [])
        return languages

    def _resolve_language(self, stage: Stage, language: str | None) -> str | None:
        """Pick the language to send to a stage engine.

        None means: let the engine use its own default language.
        """
        if stage.language:
            return stage.language
        if not language:
            return None
        entity = get_tts_entity(self.hass, stage.entity_id)
        if entity is None or not entity.supported_languages:
            return language
        supported = entity.supported_languages
        if language in supported:
            return language
        matches = language_util.matches(
            language, supported, country=self.hass.config.country
        )
        return matches[0] if matches else None

    def _unavailable_reason(self, stage: Stage) -> str | None:
        """Return why a stage can not be used at all right now, else None."""
        state = self.hass.states.get(stage.entity_id)
        if state is None or get_tts_entity(self.hass, stage.entity_id) is None:
            return "Entität nicht gefunden / nicht geladen"
        if state.state == STATE_UNAVAILABLE:
            return "Entität ist nicht verfügbar"
        return None

    # ------------------------------------------------------------------
    # Core

    async def _async_try_stage(
        self, stage: Stage, message: str, language: str | None
    ) -> tuple[str, bytes]:
        """Generate audio with a single stage. Raises on any failure."""
        media_source_id = generate_media_source_id(
            self.hass,
            message=message,
            engine=stage.entity_id,
            language=self._resolve_language(stage, language),
            options=stage.build_options(),
            # The result of the chain itself is cached by Home Assistant, no
            # need to store every stage's audio on disk as well.
            cache=False,
        )
        async with asyncio.timeout(self.timeout):
            extension, data = await async_get_media_source_audio(
                self.hass, media_source_id
            )
        if not extension or not data:
            raise HomeAssistantError("Engine returned no audio")
        return extension, data

    async def async_get_tts_audio(
        self, message: str, language: str | None
    ) -> tuple[str, bytes]:
        """Return audio from the first stage that succeeds."""
        now = dt_util.utcnow()
        ready: list[int] = []
        cooling_down: list[int] = []
        errors: list[str] = []

        for index, stage in enumerate(self.stages):
            if reason := self._unavailable_reason(stage):
                _LOGGER.debug("Skipping %s: %s", stage.entity_id, reason)
                errors.append(f"{self.stage_name(index)}: {reason}")
                continue
            if self.stats[index].in_cooldown(now):
                _LOGGER.debug(
                    "Skipping %s until %s (cooldown)",
                    stage.entity_id,
                    self.stats[index].cooldown_until,
                )
                cooling_down.append(index)
                continue
            ready.append(index)

        # Stages in cooldown are still tried as a last resort, in their order.
        for index in ready + cooling_down:
            stage = self.stages[index]
            stats = self.stats[index]
            try:
                extension, data = await self._async_try_stage(stage, message, language)
            except Exception as err:  # noqa: BLE001 - any failure means: next stage
                self._async_record_failure(index, err, message)
                errors.append(f"{self.stage_name(index)}: {_error_text(err)}")
                continue

            stats.successes += 1
            stats.cooldown_until = None
            stats.last_success_at = dt_util.utcnow()
            self.last_stage_index = index
            self.last_used_at = stats.last_success_at
            self.last_error = None
            if index != 0:
                _LOGGER.info(
                    "TTS fallback: answered by stage %s (%s)",
                    index + 1,
                    stage.entity_id,
                )
            self._async_notify()
            return extension, data

        self.last_stage_index = None
        self.last_used_at = dt_util.utcnow()
        self.last_error = "; ".join(errors) or "Keine Stufen konfiguriert"
        self.hass.bus.async_fire(
            EVENT_ALL_STAGES_FAILED,
            {"message": message[:255], "errors": errors},
        )
        self._async_notify()
        raise HomeAssistantError(f"All TTS stages failed: {self.last_error}")

    @callback
    def _async_record_failure(self, index: int, err: BaseException, message: str) -> None:
        """Remember a failed stage and put it into cooldown."""
        stage = self.stages[index]
        stats = self.stats[index]
        quota = is_quota_error(err)
        cooldown = self.quota_cooldown if quota else self.error_cooldown
        now = dt_util.utcnow()

        stats.failures += 1
        stats.last_error = _error_text(err)
        stats.last_error_at = now
        stats.cooldown_until = now + cooldown if cooldown > timedelta(0) else None

        _LOGGER.warning(
            "TTS stage %s (%s) failed%s: %s -> trying next stage%s",
            index + 1,
            stage.entity_id,
            " (quota/rate limit)" if quota else "",
            stats.last_error,
            f", skipping it for {cooldown}" if stats.cooldown_until else "",
        )
        self.hass.bus.async_fire(
            EVENT_STAGE_FAILED,
            {
                "stage": index + 1,
                "entity_id": stage.entity_id,
                "error": stats.last_error,
                "quota_error": quota,
                "cooldown_until": (
                    stats.cooldown_until.isoformat() if stats.cooldown_until else None
                ),
                "message": message[:255],
            },
        )
        self._async_notify()

    def as_dict(self) -> list[dict[str, Any]]:
        """Return per-stage information for state attributes."""
        now = dt_util.utcnow()
        return [
            {
                "stage": index + 1,
                "entity_id": stage.entity_id,
                "name": self.stage_name(index),
                "voice": stage.voice,
                "successes": stats.successes,
                "failures": stats.failures,
                "in_cooldown": stats.in_cooldown(now),
                "cooldown_until": (
                    stats.cooldown_until.isoformat() if stats.cooldown_until else None
                ),
                "last_error": stats.last_error,
                "last_error_at": (
                    stats.last_error_at.isoformat() if stats.last_error_at else None
                ),
                "last_success_at": (
                    stats.last_success_at.isoformat() if stats.last_success_at else None
                ),
            }
            for index, (stage, stats) in enumerate(zip(self.stages, self.stats, strict=True))
        ]
