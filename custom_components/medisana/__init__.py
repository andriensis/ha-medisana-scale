"""The Medisana scale integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MAX_USERS
from .coordinator import MedisanaBSCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Medisana scale from a config entry."""
    address: str = entry.data["address"]

    coordinator = MedisanaBSCoordinator(hass, address)
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload on options changes so renamed users flow through to devices.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: MedisanaBSCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to delete per-user devices from the HA UI.

    The top-level scale device can't be deleted — it represents the hardware
    itself. But empty or unused user slot devices can be, so people can clean
    up slots they don't use. If a deleted slot later receives a measurement,
    the device will be recreated automatically on the next reading.
    """
    address = config_entry.data["address"].lower()
    for domain_name, identifier in device_entry.identifiers:
        if domain_name != DOMAIN:
            continue
        # The scale's hub device identifier is exactly the address; user-slot
        # devices look like "{address}_{user_id}". Refuse deletion of the hub.
        if identifier == address:
            return False
        if identifier.startswith(f"{address}_"):
            suffix = identifier[len(address) + 1 :]
            try:
                user_id = int(suffix)
            except ValueError:
                return False
            if 1 <= user_id <= MAX_USERS:
                return True
    return False
