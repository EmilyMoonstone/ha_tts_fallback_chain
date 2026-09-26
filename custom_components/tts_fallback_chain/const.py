"""Constants for the TTS Fallback Chain integration."""

from typing import Final

DOMAIN: Final = "tts_fallback_chain"

CONF_STAGES: Final = "stages"
CONF_ENTITY_ID: Final = "entity_id"
CONF_VOICE: Final = "voice"
CONF_LANGUAGE: Final = "language"
CONF_EXTRA_OPTIONS: Final = "extra_options"
CONF_TIMEOUT: Final = "timeout"
CONF_ERROR_COOLDOWN: Final = "error_cooldown"
CONF_QUOTA_COOLDOWN: Final = "quota_cooldown"
CONF_REDEFINE_STAGES: Final = "redefine_stages"

DEFAULT_NAME: Final = "TTS Fallback-Kette"
DEFAULT_TIMEOUT: Final = 20  # seconds per stage
DEFAULT_ERROR_COOLDOWN: Final = 5  # minutes
DEFAULT_QUOTA_COOLDOWN: Final = 60  # minutes

# Substrings (lower case) in an error message that indicate an exhausted quota
# or a rate limit, e.g. Gemini's "429 RESOURCE_EXHAUSTED".
QUOTA_ERROR_MARKERS: Final = (
    "429",
    "resource_exhausted",
    "resource exhausted",
    "quota",
    "rate limit",
    "ratelimit",
    "rate_limit",
    "too many requests",
)

EVENT_STAGE_FAILED: Final = f"{DOMAIN}_stage_failed"
EVENT_ALL_STAGES_FAILED: Final = f"{DOMAIN}_all_stages_failed"
