"""Coordinator that triggers a scale session when the scale starts advertising.

The BS4xx scale only advertises (and is connectable) for a short window after
a weighing. HA's `bluetooth` component already runs a passive scanner, so we
hook into it: every time the scale's MAC address appears in a callback, we
launch one BLE session to drain its measurements.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import ADVERTISEMENT_TO_SESSION_DELAY_SECONDS, SERVICE_UUID
from .parser import UserMeasurement
from .scale import MedisanaScaleSession

_LOGGER = logging.getLogger(__name__)

# Debounce window: at most one BLE session per this many seconds. The scale
# emits multiple advertisements per second while in its BLE window, and
# weighing cycles are minutes apart at best, so 30s is a safe floor.
_SESSION_COOLDOWN_SECONDS = 30.0

# Fallback poll interval. HA's bluetooth integration sometimes stops
# dispatching callbacks for a device after the first advertisement of a
# session (the reason is under investigation — dedup of identical advert
# payloads is suspected). Polling HA's BT cache every this many seconds
# gives us a fallback path to catch subsequent weighings.
_POLL_INTERVAL_SECONDS = 20.0


MeasurementListener = Callable[[UserMeasurement], None]


class MedisanaBSCoordinator:
    """Watches advertisements and runs a BLE session on each one."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        self.hass = hass
        # Stored in lowercase for our own case-insensitive compares below.
        # Don't assume HA uses lowercase in its internal APIs — Linux HCI
        # returns uppercase MACs through BluetoothServiceInfoBleak, so any
        # match against HA-provided addresses must be done case-insensitively.
        self.address = address.lower()
        self.last_seen: float | None = None
        self.available: bool = False

        self._listeners: list[MeasurementListener] = []
        self._availability_listeners: list[Callable[[bool], None]] = []
        self._unregister_bluetooth: CALLBACK_TYPE | None = None
        # Serialize actual BLE sessions so two overlapping attempts can't
        # fight for the scale's single BLE connection.
        self._session_lock = asyncio.Lock()
        # Debounce: the scale advertises continuously while in its BLE window
        # (multiple adverts per second). We only want one session per BLE
        # window. `_last_session_scheduled_at` is monotonic time of the most
        # recent scheduled session; attempts within _SESSION_COOLDOWN of it
        # are ignored. One session reliably drains all unsynced history so
        # this doesn't lose data even if many weighings happened.
        self._last_session_scheduled_at: float = 0.0
        # Background poll task that checks HA's BT cache for the scale and
        # schedules a session if we see it. Works around cases where HA's
        # callback dispatcher doesn't fire for subsequent advertisements.
        self._poll_task: asyncio.Task[None] | None = None
        # Most recent measurement we've seen per (user_id, timestamp). We
        # dedupe across sessions so the same historical reading doesn't fire
        # listeners every time the scale wakes up.
        self._seen_keys: set[tuple[int, int]] = set()
        # Most recent UserMeasurement per user_id. Platforms adding entities
        # lazily (when a new user slot shows up in a session) read this to
        # pre-seed the entity's value with the measurement that triggered it.
        self._latest_per_user: dict[int, UserMeasurement] = {}
        # Most recent advertisement from our scale. We use service_info.device
        # directly when opening the session, so HA's internal address lookup
        # (which has been unreliable re: address case and connectable flags)
        # is never on the critical path.
        self._latest_service_info: BluetoothServiceInfoBleak | None = None

    async def async_start(self) -> None:
        _LOGGER.info(
            "Coordinator starting; listening for advertisements from scale %s",
            self.address,
        )
        # Match by service_uuid rather than by address. HA's
        # BluetoothCallbackMatcher does case-sensitive exact-string matching
        # on addresses, but the case it uses differs by platform/version
        # (Linux HCI returns uppercase MACs, macOS returns UUID strings,
        # etc.). Filtering on the Medisana service UUID and re-checking the
        # address inside the callback sidesteps that completely.
        self._unregister_bluetooth = async_register_callback(
            self.hass,
            self._on_advertisement,
            BluetoothCallbackMatcher(service_uuid=SERVICE_UUID),
            BluetoothScanningMode.ACTIVE,
        )
        self._poll_task = self.hass.async_create_task(self._poll_loop())

    async def async_stop(self) -> None:
        if self._unregister_bluetooth is not None:
            self._unregister_bluetooth()
            self._unregister_bluetooth = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    def add_listener(self, listener: MeasurementListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def add_availability_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        self._availability_listeners.append(listener)

        def _remove() -> None:
            if listener in self._availability_listeners:
                self._availability_listeners.remove(listener)

        return _remove

    @callback
    def _on_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Queue a delayed BLE session when the scale advertises.

        Matched on the Medisana service UUID rather than on address (see
        async_start). We re-check the address here to ignore other scales
        that might be in range on the same HA install.

        We intentionally do NOT connect immediately: the BS444 enters its BLE
        window before the body-composition analysis finishes, so an immediate
        sync returns only a "no data yet" heartbeat. Waiting a few seconds
        lets the weighing commit to history, then the sync pulls the fresh
        data — see ADVERTISEMENT_TO_SESSION_DELAY_SECONDS in const.py.
        """
        if service_info.address.lower() != self.address.lower():
            return  # different Medisana scale

        self.last_seen = service_info.time
        # Keep the advert around so the session can use service_info.device
        # directly instead of going through HA's address-keyed BLE cache,
        # which has had case-sensitivity and connectable-flag issues.
        self._latest_service_info = service_info
        self._set_available(True)

        now = time.monotonic()
        if now - self._last_session_scheduled_at < _SESSION_COOLDOWN_SECONDS:
            # Silent: the scale emits many adverts per second; logging each
            # one floods the log without adding signal.
            return

        _LOGGER.info(
            "Advertisement from scale %s (rssi=%s) — scheduling sync in %.0fs",
            self.address,
            service_info.rssi,
            ADVERTISEMENT_TO_SESSION_DELAY_SECONDS,
        )
        self._last_session_scheduled_at = now
        self.hass.async_create_task(self._run_delayed_session())

    async def _run_delayed_session(self) -> None:
        await asyncio.sleep(ADVERTISEMENT_TO_SESSION_DELAY_SECONDS)
        await self._run_session_locked()

    async def _poll_loop(self) -> None:
        """Backup path: poll HA's BT cache periodically for the scale.

        HA's advertisement dispatcher sometimes falls silent after the first
        callback fire — we observed subsequent weighings not dispatching
        until the integration was reloaded. Polling every
        _POLL_INTERVAL_SECONDS gives us a second path to notice the scale
        has advertised (it'll be in HA's BT cache) and trigger a sync.
        The usual cooldown keeps this from hammering the scale if the
        callback is working normally.
        """
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                now = time.monotonic()
                if now - self._last_session_scheduled_at < _SESSION_COOLDOWN_SECONDS:
                    continue
                ble_device = async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                if ble_device is None:
                    # Try again without the connectable filter — some
                    # backends don't set the flag but the device is still
                    # reachable.
                    ble_device = async_ble_device_from_address(
                        self.hass, self.address
                    )
                if ble_device is None:
                    continue
                _LOGGER.info(
                    "Poll found scale %s in HA's BT cache — scheduling sync",
                    self.address,
                )
                self._last_session_scheduled_at = now
                # Use the polled device directly rather than waiting for the
                # advert callback to populate _latest_service_info.
                self._latest_service_info = None  # force fallback to polled device
                self._polled_ble_device = ble_device
                self.hass.async_create_task(self._run_delayed_session())
        except asyncio.CancelledError:
            raise

    @callback
    def _set_available(self, available: bool) -> None:
        if self.available == available:
            return
        self.available = available
        for cb in list(self._availability_listeners):
            cb(available)

    async def _run_session_locked(self) -> None:
        """Serialize sessions — overlapping connects to the same scale fail."""
        async with self._session_lock:
            await self._run_session()

    async def _run_session(self) -> None:
        # Prefer the BLEDevice from the most recent advertisement; fall
        # back to the polled device; fall back to HA's address-keyed lookup.
        ble_device = None
        if self._latest_service_info is not None:
            ble_device = self._latest_service_info.device
        if ble_device is None:
            ble_device = getattr(self, "_polled_ble_device", None)
        if ble_device is None:
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
        if ble_device is None:
            _LOGGER.warning(
                "No BLEDevice available for %s — neither advertisement "
                "callback nor poll nor address lookup resolved it",
                self.address,
            )
            return

        _LOGGER.warning("[MEDISANA-DEBUG] Opening BLE session with %s", self.address)
        session = MedisanaScaleSession(ble_device)
        try:
            measurements = await session.fetch_measurements()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "BLE session with %s failed; will retry on next advertisement",
                self.address,
                exc_info=True,
            )
            return

        _LOGGER.warning(
            "[MEDISANA-DEBUG] BLE session with %s returned %d measurement(s)",
            self.address,
            len(measurements),
        )
        if not measurements:
            return

        new_count = 0
        for measurement in measurements:
            key = (measurement.user_id, measurement.timestamp)
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            self._latest_per_user[measurement.user_id] = measurement
            new_count += 1
            _LOGGER.warning(
                "[MEDISANA-DEBUG] New measurement: user=%s weight=%s kg "
                "fat=%s%% water=%s%% muscle=%s%% bone=%s%% kcal=%s",
                measurement.user_id,
                measurement.weight_kg,
                measurement.fat_pct,
                measurement.water_pct,
                measurement.muscle_pct,
                measurement.bone_pct,
                measurement.kcal,
            )
            for listener in list(self._listeners):
                try:
                    listener(measurement)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Measurement listener raised")
        _LOGGER.info("Dispatched %d new measurement(s) to listeners", new_count)

    def latest_for_user(self, user_id: int) -> UserMeasurement | None:
        """Return the most recent measurement stored for a given user slot."""
        return self._latest_per_user.get(user_id)

    # For platform setup: re-play cached listeners aren't needed because we
    # rely on entity restoration via RestoreEntity. Exposing this as a helper
    # for tests / diagnostics.
    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "available": self.available,
            "last_seen": self.last_seen,
            "seen_keys": sorted(self._seen_keys),
        }
