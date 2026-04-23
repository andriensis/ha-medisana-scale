"""BLE session with a Medisana BS4xx scale.

The scale is only connectable for a brief window right after a weighing. One
`fetch_measurements()` call runs the full protocol exchange: connect, subscribe
to the three indication characteristics, send the clock/trigger command, wait
for the burst of history packets, then disconnect. It returns the deduplicated
list of UserMeasurement objects.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .const import (
    CHAR_BODY,
    CHAR_COMMAND,
    CHAR_PERSON,
    CHAR_WEIGHT,
    CONNECT_TIMEOUT_SECONDS,
    POST_PACKET_QUIET_SECONDS,
)
from .parser import (
    Body,
    Person,
    UserMeasurement,
    Weight,
    build_command_packet,
)

_LOGGER = logging.getLogger(__name__)


class MedisanaScaleSession:
    """One-shot BLE session that harvests whatever the scale sends."""

    def __init__(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device

        # Incoming packets, in arrival order. We merge them at the end.
        self._persons: list[Person] = []
        self._weights: list[Weight] = []
        self._bodies: list[Body] = []

        self._last_packet_at: float = 0.0
        self._packet_event = asyncio.Event()

    async def fetch_measurements(self) -> list[UserMeasurement]:
        """Run one connect → collect → disconnect cycle.

        Returns a list of measurements, one per unique (user_id, timestamp).
        Raises nothing on "no data" — an empty list is a valid result.
        """
        name = self._ble_device.name or self._ble_device.address
        _LOGGER.info("Connecting to %s", name)
        client = await establish_connection(
            client_class=BleakClient,
            device=self._ble_device,
            name=name,
            max_attempts=3,
        )
        _LOGGER.info("Connected to %s, subscribing to indication chars", name)
        try:
            await client.start_notify(CHAR_PERSON, self._on_person)
            await client.start_notify(CHAR_WEIGHT, self._on_weight)
            await client.start_notify(CHAR_BODY, self._on_body)

            command = build_command_packet(int(time.time()))
            _LOGGER.info("Writing sync command %s to trigger history dump", command.hex())
            await client.write_gatt_char(CHAR_COMMAND, command, response=True)

            await self._wait_for_dump_to_settle()
            _LOGGER.info(
                "Dump settled: %d person / %d weight / %d body packet(s) received",
                len(self._persons),
                len(self._weights),
                len(self._bodies),
            )
        finally:
            # Even on partial data, disconnect cleanly so the scale's window
            # closes tidily and we don't block the next connection attempt.
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 — disconnect errors are noise here
                _LOGGER.debug("Disconnect raised; ignoring", exc_info=True)

        return self._merge()

    async def _wait_for_dump_to_settle(self) -> None:
        """Wait until either the overall timeout elapses or packets stop arriving.

        We give the scale up to CONNECT_TIMEOUT_SECONDS in total, but bail early
        once POST_PACKET_QUIET_SECONDS pass with no new packets after at least
        one arrived — that's the signature of a finished history dump.
        """
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS

        while True:
            remaining_total = deadline - time.monotonic()
            if remaining_total <= 0:
                return

            try:
                await asyncio.wait_for(
                    self._packet_event.wait(),
                    timeout=min(remaining_total, POST_PACKET_QUIET_SECONDS),
                )
            except asyncio.TimeoutError:
                # No packet in the quiet window. If we got anything at all,
                # the dump is done. If nothing ever arrived, keep waiting until
                # the overall timeout expires.
                if self._last_packet_at:
                    return
                continue

            self._packet_event.clear()

    # -- notification callbacks ------------------------------------------------

    def _touch(self) -> None:
        self._last_packet_at = time.monotonic()
        self._packet_event.set()

    def _on_person(self, _sender, data: bytearray) -> None:
        _LOGGER.warning("[MEDISANA-DEBUG] RAW Person (%d) %s", len(data), bytes(data).hex())
        parsed = Person.decode(bytes(data))
        if parsed is None:
            _LOGGER.warning("[MEDISANA-DEBUG] Ignored malformed Person packet: %s", data.hex())
            return
        _LOGGER.warning("[MEDISANA-DEBUG] Decoded Person: %s", parsed)
        self._persons.append(parsed)
        self._touch()

    def _on_weight(self, _sender, data: bytearray) -> None:
        _LOGGER.warning("[MEDISANA-DEBUG] RAW Weight (%d) %s", len(data), bytes(data).hex())
        parsed = Weight.decode(bytes(data))
        if parsed is None:
            _LOGGER.warning("[MEDISANA-DEBUG] Ignored malformed Weight packet: %s", data.hex())
            return
        _LOGGER.warning("[MEDISANA-DEBUG] Decoded Weight: %s", parsed)
        self._weights.append(parsed)
        self._touch()

    def _on_body(self, _sender, data: bytearray) -> None:
        _LOGGER.warning("[MEDISANA-DEBUG] RAW Body (%d) %s", len(data), bytes(data).hex())
        parsed = Body.decode(bytes(data))
        if parsed is None:
            _LOGGER.warning("[MEDISANA-DEBUG] Ignored malformed Body packet: %s", data.hex())
            return
        _LOGGER.warning("[MEDISANA-DEBUG] Decoded Body: %s", parsed)
        self._bodies.append(parsed)
        self._touch()

    # -- merge logic -----------------------------------------------------------

    def _merge(self) -> list[UserMeasurement]:
        """Stitch Person / Weight / Body packets into measurement records.

        One Weight and one Body packet share a (user_id, timestamp) when they
        come from the same weighing. Person is static per-user and has no
        timestamp of its own; we use the latest Person we saw for that user.
        """
        latest_person: dict[int, Person] = {}
        for p in self._persons:
            latest_person[p.user_id] = p  # last one wins

        # Keyed by (user_id, timestamp). Timestamp 0 means the scale's RTC was
        # unset and all readings share that key — still better than dropping.
        out: dict[tuple[int, int], UserMeasurement] = {}

        def _get(user_id: int, ts: int) -> UserMeasurement:
            key = (user_id, ts)
            existing = out.get(key)
            if existing is not None:
                return existing
            m = UserMeasurement(user_id=user_id, timestamp=ts)
            profile = latest_person.get(user_id)
            if profile is not None:
                m.is_male = profile.is_male
                m.age = profile.age
                m.height_m = profile.height_m
                m.high_activity = profile.high_activity
            out[key] = m
            return m

        for w in self._weights:
            m = _get(w.user_id, w.timestamp)
            m.weight_kg = w.weight_kg

        for b in self._bodies:
            m = _get(b.user_id, b.timestamp)
            m.kcal = b.kcal
            m.fat_pct = b.fat_pct
            m.water_pct = b.water_pct
            m.muscle_pct = b.muscle_pct
            m.bone_pct = b.bone_pct

        return sorted(out.values(), key=lambda m: (m.user_id, m.timestamp))


OnMeasurementCallback = Callable[[UserMeasurement], None]
