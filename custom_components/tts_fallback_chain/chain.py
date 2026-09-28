"""Fallback chain logic: ask the configured TTS engines one after another.

Errors and slow generations are handled differently:

* An **error** (exception from the engine, e.g. 503 or 429) means the stage is
  down. The next stage starts immediately and the failed stage is paused for a
  while (cooldown), so later announcements do not run into the same error.
* A **slow generation** (the stage's timeout elapsed without audio) is not an
  error. The slow stage keeps running, the next stage is started in parallel
  and whichever stage delivers audio first wins. Slow stages are never paused.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from time import monotonic
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
    CONF_MAX_WAIT,
    CONF_QUOTA_COOLDOWN,
    CONF_STAGES,
    CONF_TIMEOUT,
    CONF_VOICE,
    DEFAULT_ERROR_COOLDOWN,
    DEFAULT_MAX_WAIT,
    DEFAULT_QUOTA_COOLDOWN,
    DEFAULT_TIMEOUT,
    EVENT_ALL_STAGES_FAILED,
    EVENT_STAGE_FAILED,
    EVENT_STAGE_SLOW,
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
    text = str(err) or type(err).__name__
    return text if len(text) <= 300 else f"{text[:297]}..."


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True, slots=True)
class Stage:
    """One configured stage of the chain."""

    entity_id: str
    voice: str | None = None
    language: str | None = None
    extra_options: Mapping[str, Any] = field(default_factory=dict)
    timeout: float | None = None  # None = use the chain's default timeout

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Stage":
        """Create a stage from stored config entry options."""
        timeout = data.get(CONF_TIMEOUT)
        return cls(
            entity_id=data[CONF_ENTITY_ID],
            voice=data.get(CONF_VOICE) or None,
            language=data.get(CONF_LANGUAGE) or None,
            extra_options=dict(data.get(CONF_EXTRA_OPTIONS) or {}),
            timeout=float(timeout) if timeout else None,
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
    slow: int = 0
    cooldown_until: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None
    last_duration: float | None = None
    total_duration: float = 0.0

    def in_cooldown(self, now: datetime) -> bool:
        """Return True if the stage should currently be skipped."""
        return self.cooldown_until is not None and self.cooldown_until > now

    @property
    def average_duration(self) -> float | None:
        """Average generation time of successful requests in seconds."""
        return self.total_duration / self.successes if self.successes else None


class FallbackChain:
    """Holds the configuration and runtime state of one fallback chain."""

    def __init__(
        self,
        hass: HomeAssistant,
        stages: list[Stage],
        timeout: float,
        error_cooldown: timedelta,
        quota_cooldown: timedelta,
        max_wait: float = DEFAULT_MAX_WAIT,
    ) -> None:
        """Initialize the chain."""
        self.hass = hass
        self.stages = stages
        self.timeout = timeout
        self.max_wait = max_wait
        self.error_cooldown = error_cooldown
        self.quota_cooldown = quota_cooldown
        self.stats = [StageStats() for _ in stages]
        self.last_stage_index: int | None = None
        self.last_used_at: datetime | None = None
        self.last_duration: float | None = None
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
            max_wait=float(options.get(CONF_MAX_WAIT, DEFAULT_MAX_WAIT)),
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

    def stage_timeout(self, index: int) -> float:
        """Seconds to wait for a stage before starting the next one."""
        return self.stages[index].timeout or self.timeout

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

    async def _async_run_stage(
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
        extension, data = await async_get_media_source_audio(self.hass, media_source_id)
        if not extension or not data:
            raise HomeAssistantError("Engine returned no audio")
        return extension, data

    def _stage_order(self, errors: list[str]) -> list[int]:
        """Return the stage indexes to try, in order.

        Stages that are paused after an error come last, as a last resort.
        """
        now = dt_util.utcnow()
        ready: list[int] = []
        cooling_down: list[int] = []
        for index, stage in enumerate(self.stages):
            if reason := self._unavailable_reason(stage):
                _LOGGER.debug("Skipping %s: %s", stage.entity_id, reason)
                errors.append(f"{self.stage_name(index)}: {reason}")
            elif self.stats[index].in_cooldown(now):
                _LOGGER.debug(
                    "Stage %s (%s) is paused until %s, trying it last",
                    index + 1,
                    stage.entity_id,
                    self.stats[index].cooldown_until,
                )
                cooling_down.append(index)
            else:
                ready.append(index)
        return ready + cooling_down

    async def async_get_tts_audio(
        self, message: str, language: str | None
    ) -> tuple[str, bytes]:
        """Return audio from the first stage that delivers it."""
        loop = asyncio.get_running_loop()
        errors: list[str] = []
        order = self._stage_order(errors)
        deadline = loop.time() + self.max_wait

        running: dict[asyncio.Task[tuple[str, bytes]], int] = {}
        started_at: dict[int, float] = {}
        reported_slow: set[int] = set()
        next_pos = 0
        current: int | None = None  # the stage started most recently
        next_start_at = 0.0

        try:
            while True:
                now = loop.time()
                current_running = current is not None and current in running.values()

                # Start the next stage when the current one failed or is slow.
                if (
                    next_pos < len(order)
                    and now < deadline
                    and (not current_running or now >= next_start_at)
                ):
                    if current_running and current is not None:
                        self._async_record_slow(current, now - started_at[current], message)
                        reported_slow.add(current)
                    current = order[next_pos]
                    next_pos += 1
                    started_at[current] = now
                    next_start_at = now + self.stage_timeout(current)
                    task = self.hass.async_create_task(
                        self._async_run_stage(self.stages[current], message, language),
                        f"tts_fallback_chain stage {current + 1}",
                    )
                    running[task] = current
                    continue

                if not running:
                    break  # every stage failed

                wait_until = deadline
                if next_pos < len(order):
                    wait_until = min(deadline, next_start_at)
                if now >= deadline:
                    for index in running.values():
                        if index not in reported_slow:
                            self._async_record_slow(index, now - started_at[index], message)
                        errors.append(
                            f"{self.stage_name(index)}: keine Antwort nach {self.max_wait:g} s"
                        )
                    break

                done, _pending = await asyncio.wait(
                    running, timeout=wait_until - now, return_when=asyncio.FIRST_COMPLETED
                )
                finished = sorted(done, key=lambda task: running[task])
                for task in finished:
                    index = running.pop(task)
                    duration = loop.time() - started_at[index]
                    try:
                        extension, data = task.result()
                    except Exception as err:  # noqa: BLE001 - any failure: next stage
                        self._async_record_failure(index, err, duration, message)
                        errors.append(f"{self.stage_name(index)}: {_error_text(err)}")
                        continue
                    self._async_record_success(index, duration)
                    return extension, data
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        self.last_stage_index = None
        self.last_used_at = dt_util.utcnow()
        self.last_duration = None
        self.last_error = "; ".join(errors) or "Keine Stufen konfiguriert"
        self.hass.bus.async_fire(
            EVENT_ALL_STAGES_FAILED,
            {"message": message[:255], "errors": errors},
        )
        self._async_notify()
        raise HomeAssistantError(f"All TTS stages failed: {self.last_error}")

    # ------------------------------------------------------------------
    # Bookkeeping

    @callback
    def _async_record_success(self, index: int, duration: float) -> None:
        """Remember a successful generation."""
        stats = self.stats[index]
        stats.successes += 1
        stats.cooldown_until = None
        stats.last_success_at = dt_util.utcnow()
        stats.last_duration = duration
        stats.total_duration += duration
        self.last_stage_index = index
        self.last_used_at = stats.last_success_at
        self.last_duration = duration
        self.last_error = None
        log = _LOGGER.info if index != 0 else _LOGGER.debug
        log(
            "TTS answered by stage %s (%s) after %.1f s",
            index + 1,
            self.stages[index].entity_id,
            duration,
        )
        self._async_notify()

    @callback
    def _async_record_slow(self, index: int, waited: float, message: str) -> None:
        """Remember that a stage took longer than its timeout (not an error)."""
        stage = self.stages[index]
        self.stats[index].slow += 1
        _LOGGER.info(
            "TTS stage %s (%s) still generating after %.0f s, starting the next "
            "stage in parallel (no pause, the first finished audio wins)",
            index + 1,
            stage.entity_id,
            waited,
        )
        self.hass.bus.async_fire(
            EVENT_STAGE_SLOW,
            {
                "stage": index + 1,
                "entity_id": stage.entity_id,
                "waited": round(waited, 1),
                "message": message[:255],
            },
        )
        self._async_notify()

    @callback
    def _async_record_failure(
        self, index: int, err: BaseException, duration: float, message: str
    ) -> None:
        """Remember a failed stage and pause it (cooldown)."""
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
            "TTS stage %s (%s) failed after %.1f s%s: %s%s",
            index + 1,
            stage.entity_id,
            duration,
            " (quota/rate limit)" if quota else "",
            stats.last_error,
            f" -> paused for {cooldown}" if stats.cooldown_until else "",
        )
        self.hass.bus.async_fire(
            EVENT_STAGE_FAILED,
            {
                "stage": index + 1,
                "entity_id": stage.entity_id,
                "error": stats.last_error,
                "quota_error": quota,
                "duration": round(duration, 1),
                "cooldown_until": _iso(stats.cooldown_until),
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
                "timeout": self.stage_timeout(index),
                "successes": stats.successes,
                "failures": stats.failures,
                "slow": stats.slow,
                "last_duration": (
                    round(stats.last_duration, 1) if stats.last_duration is not None else None
                ),
                "average_duration": (
                    round(avg, 1) if (avg := stats.average_duration) is not None else None
                ),
                "in_cooldown": stats.in_cooldown(now),
                "cooldown_until": _iso(stats.cooldown_until),
                "last_error": stats.last_error,
                "last_error_at": _iso(stats.last_error_at),
                "last_success_at": _iso(stats.last_success_at),
            }
            for index, (stage, stats) in enumerate(zip(self.stages, self.stats, strict=True))
        ]
