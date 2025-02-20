"""Config flow for AVM FRITZ!SmartHome."""
from __future__ import annotations

from collections.abc import Mapping
import ipaddress
from typing import Any
from urllib.parse import urlparse

from pyfritzhome import Fritzhome, LoginError
from requests.exceptions import HTTPError
import voluptuous as vol

from homeassistant.components import ssdp
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import DEFAULT_HOST, DEFAULT_USERNAME, DOMAIN

DATA_SCHEMA_USER = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

DATA_SCHEMA_CONFIRM = vol.Schema(
    {
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

RESULT_INVALID_AUTH = "invalid_auth"
RESULT_NO_DEVICES_FOUND = "no_devices_found"
RESULT_NOT_SUPPORTED = "not_supported"
RESULT_SUCCESS = "success"


class FritzboxConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a AVM FRITZ!SmartHome config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._entry: ConfigEntry | None = None
        self._host: str | None = None
        self._name: str | None = None
        self._password: str | None = None
        self._username: str | None = None

    def _get_entry(self, name: str) -> FlowResult:
        return self.async_create_entry(
            title=name,
            data={
                CONF_HOST: self._host,
                CONF_PASSWORD: self._password,
                CONF_USERNAME: self._username,
            },
        )

    async def _update_entry(self) -> None:
        assert self._entry is not None
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                CONF_HOST: self._host,
                CONF_PASSWORD: self._password,
                CONF_USERNAME: self._username,
            },
        )
        await self.hass.config_entries.async_reload(self._entry.entry_id)

    def _try_connect(self) -> str:
        """Try to connect and check auth."""
        fritzbox = Fritzhome(
            host=self._host, user=self._username, password=self._password
        )
        try:
            fritzbox.login()
            fritzbox.get_device_elements()
            fritzbox.logout()
            return RESULT_SUCCESS
        except LoginError:
            return RESULT_INVALID_AUTH
        except HTTPError:
            return RESULT_NOT_SUPPORTED
        except OSError:
            return RESULT_NO_DEVICES_FOUND

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors = {}

        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})

            self._host = user_input[CONF_HOST]
            self._name = str(user_input[CONF_HOST])
            self._password = user_input[CONF_PASSWORD]
            self._username = user_input[CONF_USERNAME]

            result = await self.hass.async_add_executor_job(self._try_connect)

            if result == RESULT_SUCCESS:
                return self._get_entry(self._name)
            if result != RESULT_INVALID_AUTH:
                return self.async_abort(reason=result)
            errors["base"] = result

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA_USER, errors=errors
        )

    async def async_step_ssdp(self, discovery_info: ssdp.SsdpServiceInfo) -> FlowResult:
        """Handle a flow initialized by discovery."""
        host = urlparse(discovery_info.ssdp_location).hostname
        assert isinstance(host, str)
        self.context[CONF_HOST] = host

        if (
            ipaddress.ip_address(host).version == 6
            and ipaddress.ip_address(host).is_link_local
        ):
            return self.async_abort(reason="ignore_ip6_link_local")

        if uuid := discovery_info.upnp.get(ssdp.ATTR_UPNP_UDN):
            if uuid.startswith("uuid:"):
                uuid = uuid[5:]
            await self.async_set_unique_id(uuid)
            self._abort_if_unique_id_configured({CONF_HOST: host})

        for progress in self._async_in_progress():
            if progress.get("context", {}).get(CONF_HOST) == host:
                return self.async_abort(reason="already_in_progress")

        # update old and user-configured config entries
        for entry in self._async_current_entries():
            if entry.data[CONF_HOST] == host:
                if uuid and not entry.unique_id:
                    self.hass.config_entries.async_update_entry(entry, unique_id=uuid)
                return self.async_abort(reason="already_configured")

        self._host = host
        self._name = str(discovery_info.upnp.get(ssdp.ATTR_UPNP_FRIENDLY_NAME) or host)

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user-confirmation of discovered node."""
        errors = {}

        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            self._username = user_input[CONF_USERNAME]
            result = await self.hass.async_add_executor_job(self._try_connect)

            if result == RESULT_SUCCESS:
                assert self._name is not None
                return self._get_entry(self._name)
            if result != RESULT_INVALID_AUTH:
                return self.async_abort(reason=result)
            errors["base"] = result

        return self.async_show_form(
            step_id="confirm",
            data_schema=DATA_SCHEMA_CONFIRM,
            description_placeholders={"name": self._name},
            errors=errors,
        )

    async def async_step_reauth(self, data: Mapping[str, Any]) -> FlowResult:
        """Trigger a reauthentication flow."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        self._entry = entry
        self._host = data[CONF_HOST]
        self._name = str(data[CONF_HOST])
        self._username = data[CONF_USERNAME]

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauthorization flow."""
        errors = {}

        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            self._username = user_input[CONF_USERNAME]

            result = await self.hass.async_add_executor_job(self._try_connect)

            if result == RESULT_SUCCESS:
                await self._update_entry()
                return self.async_abort(reason="reauth_successful")
            if result != RESULT_INVALID_AUTH:
                return self.async_abort(reason=result)
            errors["base"] = result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=self._username): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"name": self._name},
            errors=errors,
        )
