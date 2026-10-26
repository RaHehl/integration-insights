"""Config flow for Integration Insights."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN
from .sampler import is_supported


class IntegrationInsightsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single entry."""
        if not is_supported():
            return self.async_abort(reason="not_supported")
        if user_input is not None:
            return self.async_create_entry(title="Integration Insights", data={})
        return self.async_show_form(step_id="user")
