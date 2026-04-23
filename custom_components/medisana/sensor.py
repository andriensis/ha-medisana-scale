"""Per-user sensor entities for the Medisana scale."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_USER_NAMES, DOMAIN, MAX_USERS
from .coordinator import MedisanaBSCoordinator
from .entity import (
    MedisanaBSScaleEntity,
    MedisanaBSUserEntity,
    resolve_user_display_name,
)
from .parser import UserMeasurement

import logging

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class MedisanaBSSensorDescription(SensorEntityDescription):
    """Describes a per-user sensor exposed by the scale."""

    # Pulls the value off a UserMeasurement; returns None when not present.
    value_fn: Callable[[UserMeasurement], float | int | str | datetime | None]


# Option C from the BMI-category design discussion — WHO standard with an
# added "slightly overweight" bucket in the 25–27 range. The labels are used
# as the raw state (translations don't load reliably for this integration).
_BMI_CATEGORIES: tuple[tuple[float, str], ...] = (
    (18.5, "Underweight"),
    (25.0, "Normal"),
    (27.0, "Slightly overweight"),
    (30.0, "Overweight"),
    (35.0, "Obese"),
)
_BMI_CATEGORY_SEVERELY_OBESE = "Severely obese"
_BMI_CATEGORY_OPTIONS: list[str] = [label for _, label in _BMI_CATEGORIES] + [
    _BMI_CATEGORY_SEVERELY_OBESE
]


def _bmi_category(bmi: float | None) -> str | None:
    if bmi is None:
        return None
    for threshold, label in _BMI_CATEGORIES:
        if bmi < threshold:
            return label
    return _BMI_CATEGORY_SEVERELY_OBESE


SENSOR_DESCRIPTIONS: tuple[MedisanaBSSensorDescription, ...] = (
    MedisanaBSSensorDescription(
        key="weight",
        name="Weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        suggested_display_precision=2,
        value_fn=lambda m: m.weight_kg,
    ),
    MedisanaBSSensorDescription(
        key="bmi",
        name="BMI",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.bmi,
    ),
    MedisanaBSSensorDescription(
        key="bmi_category",
        name="BMI category",
        device_class=SensorDeviceClass.ENUM,
        options=_BMI_CATEGORY_OPTIONS,
        value_fn=lambda m: _bmi_category(m.bmi),
    ),
    MedisanaBSSensorDescription(
        key="fat",
        name="Body fat",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda m: m.fat_pct,
    ),
    MedisanaBSSensorDescription(
        key="water",
        name="Body water",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda m: m.water_pct,
    ),
    MedisanaBSSensorDescription(
        key="muscle",
        name="Muscle",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda m: m.muscle_pct,
    ),
    MedisanaBSSensorDescription(
        key="bone",
        name="Bone",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        value_fn=lambda m: m.bone_pct,
    ),
    MedisanaBSSensorDescription(
        key="kcal",
        name="Basal metabolism",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        value_fn=lambda m: m.kcal,
    ),
    MedisanaBSSensorDescription(
        key="height",
        name="Height",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        value_fn=lambda m: round(m.height_m * 100) if m.height_m else None,
    ),
    MedisanaBSSensorDescription(
        key="age",
        name="Age",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.YEARS,
        value_fn=lambda m: m.age,
    ),
    MedisanaBSSensorDescription(
        key="last_measurement",
        name="Last measurement",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m: (
            datetime.fromtimestamp(m.timestamp, tz=timezone.utc)
            if m.timestamp
            else None
        ),
    ),
    MedisanaBSSensorDescription(
        key="gender",
        name="Gender",
        device_class=SensorDeviceClass.ENUM,
        options=["male", "female"],
        value_fn=lambda m: (
            None if m.is_male is None else ("male" if m.is_male else "female")
        ),
    ),
    MedisanaBSSensorDescription(
        key="activity_level",
        name="Activity level",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "high"],
        value_fn=lambda m: (
            None
            if m.high_activity is None
            else ("high" if m.high_activity else "normal")
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up scale-level sensors upfront; create per-user ones lazily.

    The scale has 8 configurable user slots but most users only use one or
    two — creating 8 empty devices upfront clutters the HA UI. So:

      - Scale-level "Latest weight" / "Last weighing" are created here,
        because they apply to every reading regardless of recognition.
      - Per-user entities are only created the first time a measurement
        arrives tagged with that user_id. Previously-seen slots are
        recreated on HA startup by scanning the device registry.
    """
    coordinator: MedisanaBSCoordinator = hass.data[DOMAIN][entry.entry_id]
    user_names: dict[str, str] = entry.options.get(CONF_USER_NAMES, {})
    added_users: set[int] = set()

    def _entities_for_user(user_id: int) -> list[SensorEntity]:
        display_name = resolve_user_display_name(user_names, user_id)
        return [
            MedisanaBSSensor(coordinator, user_id, description, display_name)
            for description in SENSOR_DESCRIPTIONS
        ]

    # Always-present scale-level entities.
    async_add_entities(
        [
            MedisanaBSLastWeightSensor(coordinator),
            MedisanaBSLastWeighingSensor(coordinator),
        ]
    )

    # Restore user slots that were populated in a previous HA session so the
    # RestoreEntity-backed values come back immediately after a restart.
    device_registry = dr.async_get(hass)
    prefix = f"{coordinator.address}_"
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        for domain_name, identifier in device.identifiers:
            if domain_name != DOMAIN or not identifier.startswith(prefix):
                continue
            rest = identifier[len(prefix):]
            try:
                restored_user_id = int(rest)
            except ValueError:
                continue
            if 1 <= restored_user_id <= MAX_USERS and restored_user_id not in added_users:
                added_users.add(restored_user_id)
                _LOGGER.info(
                    "Restoring entities for previously-seen user slot %d",
                    restored_user_id,
                )
                async_add_entities(_entities_for_user(restored_user_id))

    @callback
    def _on_measurement(measurement: UserMeasurement) -> None:
        uid = measurement.user_id
        _LOGGER.warning(
            "[MEDISANA-DEBUG] sensor._on_measurement fired: user=%s weight=%s added=%s",
            uid, measurement.weight_kg, sorted(added_users),
        )
        if uid < 1 or uid > MAX_USERS:
            return
        if uid in added_users:
            return
        added_users.add(uid)
        _LOGGER.warning(
            "[MEDISANA-DEBUG] First measurement for user slot %d — creating per-user entities",
            uid,
        )
        new_entities = _entities_for_user(uid)
        # Pre-seed the value on each entity so the triggering measurement
        # appears immediately, even though the entities' own listeners
        # register a moment later in async_added_to_hass.
        for entity in new_entities:
            value = entity.entity_description.value_fn(measurement)
            if value is not None:
                entity._value = value  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    entry.async_on_unload(coordinator.add_listener(_on_measurement))


class MedisanaBSSensor(MedisanaBSUserEntity, SensorEntity):
    """A single sensor value for one user on one scale."""

    entity_description: MedisanaBSSensorDescription

    def __init__(
        self,
        coordinator: MedisanaBSCoordinator,
        user_id: int,
        description: MedisanaBSSensorDescription,
        user_display_name: str,
    ) -> None:
        super().__init__(coordinator, user_id, description.key, user_display_name)
        self.entity_description = description
        self._value: float | int | str | datetime | None = None
        # Entities are enabled by default; if a given user slot is never used
        # on this scale, the entity just stays unavailable/unknown.
        self._attr_entity_registry_enabled_default = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (None, "unknown", "unavailable"):
            self._value = self._coerce_restored(last_state.state)

        self.async_on_remove(
            self._coordinator.add_listener(self._handle_measurement)
        )

        # BMI category is a derived sensor: its value is a pure function of
        # the numeric BMI sensor's value. Watch that sibling so the category
        # stays in sync whenever BMI changes — including the moment BMI
        # restores its value after an integration reload, which is the only
        # way a brand-new BMI category sensor gets populated without the
        # user having to re-weigh.
        if self.entity_description.key == "bmi_category":
            self._attach_bmi_sibling_listener()

    def _attach_bmi_sibling_listener(self) -> None:
        bmi_unique_id = f"{self._coordinator.address}_{self._user_id}_bmi"
        registry = er.async_get(self.hass)
        bmi_entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, bmi_unique_id
        )
        if bmi_entity_id is None:
            return

        # Try the current BMI state immediately in case it's already restored.
        self._apply_bmi_sibling_state(self.hass.states.get(bmi_entity_id))

        # Then subscribe so we track future BMI updates (restore race,
        # fresh measurements via the normal listener, etc.).
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [bmi_entity_id], self._on_bmi_sibling_event
            )
        )

    @callback
    def _on_bmi_sibling_event(self, event) -> None:
        self._apply_bmi_sibling_state(event.data.get("new_state"))

    @callback
    def _apply_bmi_sibling_state(self, state: State | None) -> None:
        if state is None or state.state in (None, "unknown", "unavailable"):
            return
        try:
            category = _bmi_category(float(state.state))
        except (TypeError, ValueError):
            return
        if category is None or category == self._value:
            return
        self._value = category
        self.async_write_ha_state()

    def _coerce_restored(self, raw: str) -> Any:
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        # Numeric restore: fall back to raw string if it's not a number.
        try:
            if "." in raw:
                return float(raw)
            return int(raw)
        except ValueError:
            return raw

    @callback
    def _handle_measurement(self, measurement: UserMeasurement) -> None:
        if measurement.user_id != self._user_id:
            return
        new_value = self.entity_description.value_fn(measurement)
        if new_value is None:
            return
        self._value = new_value
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        return self._value

    def _has_state_value(self) -> bool:
        return self._value is not None


class MedisanaBSLastWeightSensor(MedisanaBSScaleEntity, SensorEntity):
    """Scale-level 'latest weight' sensor — updates on any reading.

    Unlike the per-user weight sensors, this one updates for every weighing
    the scale transmits, including anonymous / guest weighings where the
    scale didn't attribute the reading to user slots 1–8. Useful if you
    step on with shoes, step off before the body-composition cycle
    finishes, or the scale just fails to auto-recognise you.
    """

    _attr_name = "Latest weight"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: MedisanaBSCoordinator) -> None:
        super().__init__(coordinator, "last_weight")
        self._value: float | None = None
        # Track the timestamp of the reading we're displaying so out-of-order
        # history replays don't cause the sensor to regress to an older value.
        self._latest_ts: int = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None, "unknown", "unavailable"
        ):
            try:
                self._value = float(last_state.state)
            except (TypeError, ValueError):
                self._value = None

        self.async_on_remove(self._coordinator.add_listener(self._handle_measurement))

    @callback
    def _handle_measurement(self, measurement: UserMeasurement) -> None:
        if measurement.weight_kg is None:
            return
        if measurement.timestamp and measurement.timestamp < self._latest_ts:
            return
        self._latest_ts = measurement.timestamp or self._latest_ts
        self._value = measurement.weight_kg
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        return self._value

    def _has_state_value(self) -> bool:
        return self._value is not None


class MedisanaBSLastWeighingSensor(MedisanaBSScaleEntity, SensorEntity):
    """Timestamp of the most recent weighing the scale reported."""

    _attr_name = "Last weighing"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MedisanaBSCoordinator) -> None:
        super().__init__(coordinator, "last_weighing")
        self._value: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None, "unknown", "unavailable"
        ):
            try:
                self._value = datetime.fromisoformat(last_state.state)
            except (TypeError, ValueError):
                self._value = None

        self.async_on_remove(self._coordinator.add_listener(self._handle_measurement))

    @callback
    def _handle_measurement(self, measurement: UserMeasurement) -> None:
        if not measurement.timestamp:
            return
        incoming = datetime.fromtimestamp(measurement.timestamp, tz=timezone.utc)
        if self._value is not None and incoming < self._value:
            return
        self._value = incoming
        self.async_write_ha_state()

    @property
    def native_value(self) -> Any:
        return self._value

    def _has_state_value(self) -> bool:
        return self._value is not None
