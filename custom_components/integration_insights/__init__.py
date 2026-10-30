"""Integration Insights: show how much each integration loads Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_loaded_integration

from . import panel, websocket
from .const import DOMAIN
from .insights import Insights
from .sampler import SamplerError, is_supported

type IntegrationInsightsConfigEntry = ConfigEntry[Insights]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the WebSocket API."""
    websocket.async_setup(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: IntegrationInsightsConfigEntry
) -> bool:
    """Set up Integration Insights from a config entry."""
    if not is_supported():
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="not_supported"
        )
    insights = Insights(hass)
    entry.async_on_unload(insights.stop)
    try:
        insights.start()
    except SamplerError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="sampler_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = insights
    version = async_get_loaded_integration(hass, DOMAIN).version
    await panel.async_register_panel(hass, str(version))
    entry.async_on_unload(lambda: panel.async_remove_panel(hass))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: IntegrationInsightsConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
