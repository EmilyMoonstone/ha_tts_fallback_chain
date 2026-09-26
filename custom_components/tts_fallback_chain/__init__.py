"""TTS Fallback Chain: one TTS entity that tries several TTS engines in order."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .chain import FallbackChain

PLATFORMS: list[Platform] = [Platform.TTS, Platform.SENSOR, Platform.BUTTON]

type FallbackChainConfigEntry = ConfigEntry[FallbackChain]


async def async_setup_entry(hass: HomeAssistant, entry: FallbackChainConfigEntry) -> bool:
    """Set up a fallback chain from a config entry."""
    entry.runtime_data = FallbackChain.from_options(hass, entry.options)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FallbackChainConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: FallbackChainConfigEntry) -> None:
    """Reload the entry after the options were changed."""
    await hass.config_entries.async_reload(entry.entry_id)
