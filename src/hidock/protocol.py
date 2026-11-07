"""Jensen framing, independent of USB transfer boundaries."""

from dataclasses import dataclass
from datetime import datetime
import struct

from .errors import ProtocolError

HEADER = struct.Struct(">HHII")
MAX_BODY = 1024 * 1024


@dataclass(frozen=True)
class RecordingQuality:
    quality: str
    raw_code: int


def parse_recording_quality(body: bytes) -> RecordingQuality:
    if len(body) != 4:
        raise ProtocolError("Recording quality response must contain 4 bytes")
    code = int.from_bytes(body, "big")
    return RecordingQuality({0: "normal", 1: "high", 2: "normal"}.get(code, "unknown"), code)


SETTING_FIELDS = ("auto_record", "auto_play", "notification", "bluetooth_tone", "record_on_vibe")


@dataclass(frozen=True)
class DeviceSettings:
    auto_record: bool | None
    auto_play: bool | None
    notification: bool | None
    bluetooth_tone: bool | None
    record_on_vibe: bool | None
    raw_values: tuple[int, ...]


def parse_device_settings(body: bytes) -> DeviceSettings:
    if len(body) < 8 or len(body) % 4:
        raise ProtocolError("Settings response must contain at least two complete uint32 fields")
    words = tuple(int.from_bytes(body[i:i + 4], "big") for i in range(0, len(body), 4))
    values = []
    for index, field in enumerate(SETTING_FIELDS):
        code = words[index] if index < len(words) else None
        value = {1: True, 2: False}.get(code)
        if field == "bluetooth_tone" and value is not None:
            value = not value
        values.append(value)
    return DeviceSettings(*values, words)


def encode_settings(changes: dict[str, bool | None]) -> bytes:
    unknown = set(changes) - set(SETTING_FIELDS)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}")
    updates = {key: value for key, value in changes.items() if value is not None}
    if not updates:
        raise ValueError("Supply at least one setting to change")
    if any(type(value) is not bool for value in updates.values()):
        raise ValueError("Settings must be bool or None")
    count = max(SETTING_FIELDS.index(key) for key in updates) + 1
    words = [0] * count  # Zero means leave that setting unchanged.
    for key, value in updates.items():
        if key == "bluetooth_tone":
            value = not value
        words[SETTING_FIELDS.index(key)] = 1 if value else 2
    return b"".join(word.to_bytes(4, "big") for word in words)


def check_acknowledgement(body: bytes, operation: str) -> None:
    if len(body) != 1:
        raise ProtocolError(f"Invalid {operation} acknowledgement")
    if body[0] != 0:
        raise ProtocolError(f"Device rejected {operation} (code {body[0]})")


def parse_device_time(body: bytes) -> datetime | None:
    """Decode seven BCD bytes as a timezone-free device wall clock."""
    if len(body) != 7:
        raise ProtocolError("Device time response must contain 7 BCD bytes")
    if body == bytes(7):
        return None
    if any(b >> 4 > 9 or b & 15 > 9 for b in body):
        raise ProtocolError("Invalid BCD digit in device time")
    parts = [(b >> 4) * 10 + (b & 15) for b in body]
    try:
        return datetime(parts[0] * 100 + parts[1], *parts[2:])
    except ValueError as exc:
        raise ProtocolError(f"Invalid device date/time: {exc}") from exc


def encode_device_time(value: datetime) -> bytes:
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime")
    # Preserve the supplied wall clock; the protocol has no UTC offset field.
    digits = f"{value.year:04d}{value.month:02d}{value.day:02d}{value.hour:02d}{value.minute:02d}{value.second:02d}"
    return bytes((int(digits[i]) << 4) | int(digits[i + 1]) for i in range(0, 14, 2))


@dataclass(frozen=True)
class RecordingStatus:
    is_recording: bool
    recording: str | None
    duration_seconds: int
    type: str | None
    type_code: int | None
    samples: tuple[int, ...]


def parse_recording_status(body: bytes) -> RecordingStatus:
    if not body:
        return RecordingStatus(False, None, 0, None, None, ())
    if len(body) < 5:
        raise ProtocolError("Truncated recording status response")
    type_code, name_length = body[:2]
    end = 2 + name_length
    if len(body) < end + 3:
        raise ProtocolError("Truncated recording status filename or duration")
    try:
        name = body[2:end].decode("utf-8").rstrip("\0")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Invalid recording status filename") from exc
    duration = int.from_bytes(body[end:end + 2], "big")
    sample_count = body[end + 2]
    if len(body) != end + 3 + sample_count:
        raise ProtocolError("Recording status sample count does not match payload")
    kind = {0: "recording", 1: "whisper"}.get(type_code, "unknown")
    return RecordingStatus(bool(name), name or None, duration, kind, type_code,
                           tuple(body[end + 3:]))


@dataclass(frozen=True)
class StorageInfo:
    total_bytes: int
    free_bytes: int
    used_bytes: int
    status_code: int

    @property
    def used_percent(self) -> float:
        return self.used_bytes / self.total_bytes * 100


def parse_storage_info(body: bytes) -> StorageInfo:
    """P1 card information: free MiB, capacity MiB, opaque status (all BE)."""
    if len(body) < 12:
        raise ProtocolError(f"Truncated storage information response: {len(body)} bytes (expected at least 12)")
    free_mib, total_mib, status_code = struct.unpack_from(">III", body)
    # Reject implausible units/layouts rather than guessing firmware formats.
    if not 100 <= total_mib <= 256 * 1024:
        raise ProtocolError(f"Invalid storage capacity: {total_mib} MiB")
    if free_mib > total_mib:
        raise ProtocolError("Free storage exceeds total capacity")
    mib = 1024 * 1024
    return StorageInfo(total_mib * mib, free_mib * mib,
                       (total_mib - free_mib) * mib, status_code)


@dataclass(frozen=True)
class BatteryStatus:
    status: str
    percent: int
    voltage_microvolts: int
    status_code: int

    @property
    def voltage_volts(self) -> float:
        return self.voltage_microvolts / 1_000_000


def parse_battery_status(body: bytes) -> BatteryStatus:
    """P1 battery payload: state, percent, big-endian uint32 microvolts."""
    if len(body) != 6:
        raise ProtocolError(f"Invalid battery status response length: {len(body)} (expected 6)")
    status_code, percent, voltage = struct.unpack(">BBI", body)
    if percent > 100:
        raise ProtocolError(f"Invalid battery percentage: {percent}")
    status = {0: "idle", 1: "charging", 2: "full"}.get(status_code, "unknown")
    return BatteryStatus(status, percent, voltage, status_code)


@dataclass(frozen=True)
class Frame:
    command: int
    sequence: int
    body: bytes


def encode(command: int, sequence: int, body: bytes = b"") -> bytes:
    if len(body) > 0xFFFFFF:
        raise ValueError("Command payload is too large")
    return HEADER.pack(0x1234, command, sequence, len(body)) + body


class FrameReader:
    def __init__(self):
        self.buffer = bytearray()

    def pop(self) -> Frame | None:
        if len(self.buffer) < HEADER.size:
            return None
        magic, command, sequence, length = HEADER.unpack_from(self.buffer)
        if magic != 0x1234:
            raise ProtocolError("Invalid USB response header")
        padding, size = length >> 24, length & 0xFFFFFF
        if size > MAX_BODY:
            raise ProtocolError(f"Response frame too large: {size}")
        total = HEADER.size + size + padding
        if len(self.buffer) < total:
            return None
        frame = Frame(command, sequence, bytes(self.buffer[HEADER.size:HEADER.size + size]))
        del self.buffer[:total]
        return frame


@dataclass(frozen=True)
class Recording:
    name: str
    size: int
    version: int
    signature: str


def parse_recordings(body: bytes) -> list[Recording]:
    offset = 0
    expected = None
    if body.startswith(b"\xff\xff"):
        if len(body) < 6:
            raise ProtocolError("Truncated file list header")
        expected = int.from_bytes(body[2:6], "big")
        offset = 6
    recordings = []
    while offset < len(body):
        if len(body) - offset < 4:
            raise ProtocolError("Truncated file list entry")
        version = body[offset]
        name_size = int.from_bytes(body[offset + 1:offset + 4], "big")
        offset += 4
        if not 0 < name_size <= 1024 or offset + name_size + 26 > len(body):
            raise ProtocolError("Truncated or invalid file list entry")
        try:
            name = body[offset:offset + name_size].rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Invalid recording filename") from exc
        offset += name_size
        size = int.from_bytes(body[offset:offset + 4], "big")
        signature = body[offset + 10:offset + 26].hex()
        offset += 26
        recordings.append(Recording(name, size, version, signature))
    if expected is not None and len(recordings) != expected:
        raise ProtocolError(f"Incomplete file list: expected {expected}, got {len(recordings)}")
    if len({r.name for r in recordings}) != len(recordings):
        raise ProtocolError("Duplicate recording names in file list")
    return recordings
