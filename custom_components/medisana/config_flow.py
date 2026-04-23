"""Config flow for the Medisana scale.

Two entry points:

1. `async_step_bluetooth` — HA's Bluetooth integration auto-dispatches here
   when the scale advertises (matched by the service UUID in manifest.json).
   The user just confirms.

2. `async_step_user` — user picks "Medisana" from the add-integration
   list. We show a "step on your scale" prompt, then actively scan for an
   advertisement with the scale's service UUID. Found → confirm step. Not
   found within the window → show a "not found, try again" error.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_process_advertisements,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .const import CONF_USER_NAMES, DOMAIN, MAX_USERS, SERVICE_UUID

_LOGGER = logging.getLogger(__name__)

# How long to wait for the scale to advertise during the user-triggered flow.
# Covers one full weighing: step on, scale settles, measurement done, advert.
SCAN_TIMEOUT_SECONDS = 60


def _service_info_matches_scale(info: BluetoothServiceInfoBleak) -> bool:
    return SERVICE_UUID in (info.service_uuids or [])


class MedisanaBSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Medisana scale."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> MedisanaBSOptionsFlow:
        return MedisanaBSOptionsFlow()

    def __init__(self) -> None:
        self._discovered: BluetoothServiceInfoBleak | None = None

    # -- entry point 1: advertisement-triggered --------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Bluetooth discovery dispatched by HA."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovered = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or "Medisana scale",
            "address": discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovered is not None
        if user_input is not None:
            return self._create_entry(self._discovered)

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovered.name or "Medisana scale",
                "address": self._discovered.address,
            },
            data_schema=vol.Schema({}),
        )

    # -- entry point 2: user-initiated -----------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First screen: check if the scale is already advertising; otherwise
        prompt the user to step on it and start scanning."""

        # Already-visible fast path: if HA's passive scanner has seen the scale
        # very recently, skip the "step on" prompt and go straight to confirm.
        for info in async_discovered_service_info(self.hass, connectable=True):
            if not _service_info_matches_scale(info):
                continue
            unique_id = format_mac(info.address)
            if unique_id in self._async_current_ids():
                continue
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            self._discovered = info
            return await self.async_step_bluetooth_confirm()

        # Nothing seen yet; show the instruction screen.
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
            )

        return await self.async_step_scanning()

    async def async_step_scanning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Actively wait up to SCAN_TIMEOUT_SECONDS for the scale to advertise."""
        matcher = BluetoothCallbackMatcher(
            connectable=True,
            service_uuid=SERVICE_UUID,
        )
        try:
            info = await async_process_advertisements(
                self.hass,
                self._async_is_fresh_scale,
                matcher,
                BluetoothScanningMode.ACTIVE,
                SCAN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
                errors={"base": "scale_not_found"},
            )

        await self.async_set_unique_id(format_mac(info.address))
        self._abort_if_unique_id_configured()
        self._discovered = info
        return await self.async_step_bluetooth_confirm()

    def _async_is_fresh_scale(self, info: BluetoothServiceInfoBleak) -> bool:
        if not _service_info_matches_scale(info):
            return False
        if format_mac(info.address) in self._async_current_ids():
            return False
        return True

    # -- finalize --------------------------------------------------------------

    def _create_entry(self, info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        title = info.name or f"Medisana scale ({info.address})"
        return self.async_create_entry(
            title=title,
            data={"address": info.address},
        )


class MedisanaBSOptionsFlow(OptionsFlow):
    """Lets the user assign a friendly display name to each of the 8 user slots.

    The scale tracks users internally as IDs 1..8. A name entered here becomes
    the HA device name for that user's sub-device, which in turn shows up as
    the prefix on every per-user entity (weight, BMI, body fat, …).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            cleaned = {
                str(i): (user_input.get(f"user_{i}") or "").strip()
                for i in range(1, MAX_USERS + 1)
            }
            # Drop empty strings so we fall back to the default naming.
            cleaned = {k: v for k, v in cleaned.items() if v}
            return self.async_create_entry(data={CONF_USER_NAMES: cleaned})

        current = self.config_entry.options.get(CONF_USER_NAMES, {})
        schema_dict: dict[Any, Any] = {}
        for i in range(1, MAX_USERS + 1):
            key = vol.Optional(
                f"user_{i}",
                description={"suggested_value": current.get(str(i), "")},
            )
            schema_dict[key] = TextSelector(TextSelectorConfig())

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
