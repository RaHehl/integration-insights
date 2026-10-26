"""Config flow for Integration Insights."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class IntegrationInsightsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single entry."""
        if user_input is not None:
            return self.async_create_entry(title="Integration Insights", data={})
        return self.async_show_form(step_id="user")
