"""Shared base classes for Medisana entities."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MedisanaBSCoordinator


def resolve_user_display_name(user_names: dict[str, str], user_id: int) -> str:
    """Pick the device-level display name for a scale user slot.

    If the user has set a friendly name in the options flow, use that verbatim.
    Otherwise fall back to a generic "Medisana scale user N" label so the
    device is identifiable in the HA UI before any data arrives.
    """
    custom = (user_names or {}).get(str(user_id), "").strip()
    return custom or f"Medisana scale user {user_id}"


class MedisanaBSScaleEntity(RestoreEntity, Entity):
    """Base class for scale-level entities (not tied to a user slot).

    These entities live on the top-level Medisana scale device rather than
    one of its per-user sub-devices, so they can react to any reading —
    including anonymous / guest weighings where the scale didn't attribute
    the measurement to a specific user slot (user_id = 0xFF).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: MedisanaBSCoordinator,
        key: str,
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_scale_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            name="Medisana scale",
            manufacturer=MANUFACTURER,
            model="BS4xx",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.add_availability_listener(self._handle_availability)
        )

    def _handle_availability(self, available: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.available or self._has_state_value()

    def _has_state_value(self) -> bool:
        return False


class MedisanaBSUserEntity(RestoreEntity, Entity):
    """Base class for per-user entities on a Medisana scale.

    The scale only wakes up after a weighing, so entities spend most of their
    life "unavailable" from HA's point of view. We inherit from RestoreEntity
    so the last-known value survives restarts.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: MedisanaBSCoordinator,
        user_id: int,
        key: str,
        user_display_name: str,
    ) -> None:
        self._coordinator = coordinator
        self._user_id = user_id
        self._attr_unique_id = f"{coordinator.address}_{user_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}_{user_id}")},
            name=user_display_name,
            manufacturer=MANUFACTURER,
            model="BS4xx",
            via_device=(DOMAIN, coordinator.address),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.add_availability_listener(self._handle_availability)
        )

    def _handle_availability(self, available: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # The scale is considered available once we've seen at least one
        # advertisement in this HA session. If we've also already restored a
        # previous state, surface that too.
        return self._coordinator.available or self._has_state_value()

    def _has_state_value(self) -> bool:
        # Override in subclasses if they track their own cached value.
        return False
