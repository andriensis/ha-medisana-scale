from __future__ import annotations

import struct
from dataclasses import dataclass

from .const import SCALE_EPOCH_OFFSET

PERSON_VALIDITY = 0x84
WEIGHT_VALIDITY = 0x1D
BODY_VALIDITY = 0x6F


def _unix_timestamp(raw: int) -> int:
    """Map the scale's 2010-epoch timestamp to Unix epoch.

    The scale emits seconds since 2010-01-01 UTC. `raw == 0` means the scale's
    RTC was never set, in which case we return 0 and let the caller ignore it.
    """
    if raw <= 0:
        return 0
    return raw + SCALE_EPOCH_OFFSET


@dataclass(frozen=True)
class Person:
    """User profile packet (characteristic 0x8a82)."""

    user_id: int  # 1..8
    is_male: bool
    age: int  # years
    height_m: float
    high_activity: bool

    @classmethod
    def decode(cls, data: bytes) -> Person | None:
        if len(data) < 9 or data[0] != PERSON_VALIDITY:
            return None
        return cls(
            user_id=data[2],
            is_male=data[4] == 1,
            age=data[5],
            height_m=data[6] / 100.0,
            high_activity=data[8] == 3,
        )


@dataclass(frozen=True)
class Weight:
    """Weight measurement packet (characteristic 0x8a21)."""

    user_id: int  # 1..8
    weight_kg: float
    timestamp: int  # Unix seconds; 0 if the scale's RTC was unset

    @classmethod
    def decode(cls, data: bytes) -> Weight | None:
        if len(data) < 14 or data[0] != WEIGHT_VALIDITY:
            return None
        raw_weight = struct.unpack_from("<H", data, 1)[0]
        raw_ts = struct.unpack_from("<I", data, 5)[0]
        return cls(
            user_id=data[13],
            weight_kg=raw_weight / 100.0,
            timestamp=_unix_timestamp(raw_ts),
        )


@dataclass(frozen=True)
class Body:
    """Body composition packet (characteristic 0x8a22)."""

    user_id: int
    timestamp: int
    kcal: int
    fat_pct: float
    water_pct: float
    muscle_pct: float
    bone_pct: float

    @classmethod
    def decode(cls, data: bytes) -> Body | None:
        if len(data) < 16 or data[0] != BODY_VALIDITY:
            return None
        raw_ts = struct.unpack_from("<I", data, 1)[0]
        kcal, fat, water, muscle, bone = struct.unpack_from("<HHHHH", data, 6)
        # The top nibble of each composition field is a tag (0xf); mask it off.
        return cls(
            user_id=data[5],
            timestamp=_unix_timestamp(raw_ts),
            kcal=kcal,
            fat_pct=(fat & 0x0FFF) / 10.0,
            water_pct=(water & 0x0FFF) / 10.0,
            muscle_pct=(muscle & 0x0FFF) / 10.0,
            bone_pct=(bone & 0x0FFF) / 10.0,
        )


@dataclass
class UserMeasurement:
    """A single complete measurement for one user, assembled from packets."""

    user_id: int
    timestamp: int  # Unix seconds; 0 if the scale had no RTC set

    # From Person (static profile — may be missing if the user didn't step on
    # long enough for a Person packet to be sent):
    is_male: bool | None = None
    age: int | None = None
    height_m: float | None = None
    high_activity: bool | None = None

    # From Weight:
    weight_kg: float | None = None

    # From Body:
    kcal: int | None = None
    fat_pct: float | None = None
    water_pct: float | None = None
    muscle_pct: float | None = None
    bone_pct: float | None = None

    @property
    def bmi(self) -> float | None:
        if self.weight_kg is None or not self.height_m:
            return None
        return self.weight_kg / (self.height_m * self.height_m)


def build_command_packet(unix_now: int) -> bytes:
    """Build the 5-byte command that syncs the clock and triggers a dump.

    The scale expects seconds since its 2010 epoch, little-endian u32, prefixed
    with the command opcode 0x02.
    """
    scale_ts = max(0, unix_now - SCALE_EPOCH_OFFSET)
    return struct.pack("<BI", 0x02, scale_ts)
