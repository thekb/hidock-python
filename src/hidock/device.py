"""Device API. Instances serialize USB operations; writes are explicit."""

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable

from .errors import HiDockError, ProtocolError, TransferTimeoutError
from .protocol import (BatteryStatus, DeviceSettings, FrameReader, Recording, RecordingQuality,
                       RecordingStatus, SETTING_FIELDS, StorageInfo, check_acknowledgement,
                       encode, encode_device_time, encode_settings, parse_battery_status,
                       parse_device_settings, parse_recording_quality,
                       parse_device_time, parse_recording_status, parse_recordings,
                       parse_storage_info)
from .transport import USBTransport
from .transcription import Transcriber


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    firmware: str
    serial: str


def _filename(name: str) -> bytes:
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\0")):
        raise ValueError("Expected a single recording filename")
    data = name.encode("utf-8")
    if len(data) > 255:
        raise ValueError("Recording filename is too long")
    return data


class HiDock:
    """Connect to a P1 using ``with HiDock() as dock: ...``.

    ``timeout`` is the maximum idle wait per frame in seconds. Clock, quality,
    settings, and recording deletion change only through explicit write methods.
    """

    def __init__(self, *, serial: str | None = None, timeout: float = 10,
                 transport=None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._transport = transport if transport is not None else USBTransport(serial=serial)
        self._timeout = timeout
        self._reader = FrameReader()
        self._sequence = 0
        self._lock = threading.RLock()
        self._connected = False

    def connect(self):
        with self._lock:
            if not self._connected:
                self._transport.open()
                self._reader = FrameReader()
                self._sequence = 0
                self._connected = True
        return self

    def close(self):
        with self._lock:
            self._connected = False
            self._reader = FrameReader()
            self._transport.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()

    def _send(self, command, body=b""):
        if not self._connected:
            raise HiDockError("Device is not connected; use 'with HiDock() as dock:'")
        self._sequence = self._sequence % 0xFFFFFFFF + 1
        self._transport.write(encode(command, self._sequence, body), int(self._timeout * 1000))
        return self._sequence

    def _receive(self, command, sequence):
        deadline = time.monotonic() + self._timeout
        while True:
            frame = self._reader.pop()
            if frame is not None:
                if frame.command == command and frame.sequence == sequence:
                    return frame.body
                # Notifications and replies to an earlier command may arrive here.
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransferTimeoutError("Timed out waiting for a matching HiDock response")
                self._reader.buffer.extend(self._transport.read(max(1, int(remaining * 1000))))
            if time.monotonic() >= deadline:
                raise TransferTimeoutError("Timed out waiting for a matching HiDock response")

    def info(self) -> DeviceInfo:
        with self._lock:
            body = self._receive(1, self._send(1))
            if len(body) < 4:
                raise ProtocolError("Truncated device information")
            return DeviceInfo("HiDock P1", ".".join(str(b) for b in body[1:4]),
                              body[4:].rstrip(b"\0").decode("ascii", errors="replace"))

    def get_time(self) -> datetime | None:
        """Read the device wall clock (no timezone); None means uninitialized."""
        with self._lock:
            return parse_device_time(self._receive(2, self._send(2)))

    def set_time(self, value: datetime | None = None) -> datetime:
        """Set the wall clock, defaulting to the computer's current local time.

        Supplied timezone-aware values keep their wall-clock components; the
        device stores neither timezone nor fractions of a second. Returns the
        acknowledged requested time, not a subsequent readback.
        """
        value = datetime.now() if value is None else value
        payload = encode_device_time(value)
        with self._lock:
            body = self._receive(3, self._send(3, payload))
            if len(body) != 1:
                raise ProtocolError("Invalid set-time acknowledgement")
            if body[0] != 0:
                raise ProtocolError(f"Device rejected clock update (code {body[0]})")
        return value.replace(tzinfo=None, microsecond=0)

    def recording_status(self) -> RecordingStatus:
        """Read the current recording snapshot without starting or stopping it."""
        with self._lock:
            return parse_recording_status(self._receive(0x1D, self._send(0x1D)))

    def get_recording_quality(self) -> RecordingQuality:
        with self._lock:
            return parse_recording_quality(self._receive(0x1F, self._send(0x1F)))

    def set_recording_quality(self, quality: str) -> RecordingQuality:
        """Set normal/high quality; check acknowledgement and return readback."""
        if quality not in ("normal", "high"):
            raise ValueError("Recording quality must be 'normal' or 'high'")
        with self._lock:
            code = 1 if quality == "high" else 0
            if quality == "normal":
                version = tuple(int(part) for part in self.info().firmware.split("."))
                if version < (1, 4, 6):
                    code = 2
            check_acknowledgement(self._receive(0x1E, self._send(0x1E, code.to_bytes(4, "big"))),
                                  "recording quality update")
            actual = self.get_recording_quality()
            if actual.quality != quality:
                raise ProtocolError("Quality update was acknowledged but readback differs; inspect device state")
            return actual

    def get_settings(self) -> DeviceSettings:
        with self._lock:
            return parse_device_settings(self._receive(0x0B, self._send(0x0B)))

    def set_settings(self, *, auto_record: bool | None = None, auto_play: bool | None = None,
                     notification: bool | None = None, bluetooth_tone: bool | None = None,
                     record_on_vibe: bool | None = None) -> DeviceSettings:
        """Change only supplied fields and return readback. None leaves a field alone."""
        changes = dict(auto_record=auto_record, auto_play=auto_play, notification=notification,
                       bluetooth_tone=bluetooth_tone, record_on_vibe=record_on_vibe)
        payload = encode_settings(changes)
        with self._lock:
            current = self.get_settings()
            for key, value in changes.items():
                if value is not None and SETTING_FIELDS.index(key) >= len(current.raw_values):
                    raise HiDockError(f"Device firmware does not expose setting: {key}")
            check_acknowledgement(self._receive(0x0C, self._send(0x0C, payload)), "settings update")
            actual = self.get_settings()
            if any(value is not None and getattr(actual, key) != value for key, value in changes.items()):
                raise ProtocolError("Settings update was acknowledged but readback differs; inspect device state")
            return actual

    def delete_recording(self, recording: Recording | str, *, missing_ok: bool = False) -> bool:
        """Permanently delete one exact device filename, refusing an active file.

        Returns True if deleted. With missing_ok, a missing file returns False.
        This never deletes a local downloaded file or transcript.
        """
        name = recording.name if isinstance(recording, Recording) else recording
        payload = _filename(name)
        with self._lock:
            if name not in {r.name for r in self.list_recordings()}:
                if missing_ok:
                    return False
                raise FileNotFoundError(f"Recording not found on HiDock: {name}")
            status = self.recording_status()
            if status.is_recording and status.recording == name:
                raise HiDockError(f"Cannot delete an active recording: {name}")
            body = self._receive(7, self._send(7, payload))
            if body == b"\x01":
                if missing_ok:
                    return False
                raise FileNotFoundError(f"Recording not found on HiDock: {name}")
            check_acknowledgement(body, "recording deletion")
            return True

    def battery_status(self) -> BatteryStatus:
        """Read charge state, percentage, and voltage without changing settings."""
        with self._lock:
            return parse_battery_status(self._receive(0x1004, self._send(0x1004)))

    def storage_info(self) -> StorageInfo:
        """Read total, free, and used storage in bytes and the raw card status."""
        with self._lock:
            return parse_storage_info(self._receive(0x10, self._send(0x10)))

    def file_count(self) -> int:
        with self._lock:
            body = self._receive(6, self._send(6))
            if len(body) != 4:
                raise ProtocolError("Invalid file count response")
            return int.from_bytes(body, "big")

    def list_recordings(self) -> list[Recording]:
        with self._lock:
            count = self.file_count()
            sequence = self._send(4)
            body = bytearray(self._receive(4, sequence))
            # Current P1 firmware supplies a count prefix and 57-byte entries
            # split over 8180-byte frames. Parse lengths, never scan audio/magic.
            expected = 6 if body.startswith(b"\xff\xff") else 0
            if expected:
                if len(body) < 6:
                    raise ProtocolError("Truncated file list header")
                count = int.from_bytes(body[2:6], "big")
            for _ in range(count):
                while len(body) < expected + 4:
                    body.extend(self._receive(4, sequence))
                name_size = int.from_bytes(body[expected + 1:expected + 4], "big")
                if not 0 < name_size <= 1024:
                    raise ProtocolError("Invalid file list filename length")
                expected += 4 + name_size + 26
                while len(body) < expected:
                    body.extend(self._receive(4, sequence))
            return parse_recordings(bytes(body))

    def download(self, recording: Recording | str, destination: str | Path, *,
                 overwrite: bool = False,
                 progress: Callable[[int, int], None] | None = None,
                 transcriber: Transcriber | None = None) -> Path:
        """Stream exact device bytes to a file; publish only a complete transfer.

        ``destination`` is a file path. Progress receives (bytes_saved, total).
        Original .hda bytes are preserved; no format conversion is performed.
        If supplied, transcriber writes a .txt sidecar after the audio is saved.
        A transcription failure leaves the completed audio available for retry.
        """
        name = recording.name if isinstance(recording, Recording) else recording
        payload = _filename(name)
        path = Path(destination)
        with self._lock:
            if path.exists() and not overwrite:
                raise FileExistsError(path)
            # Resolve fresh metadata even for a Recording object supplied by callers.
            entries = {r.name: r for r in self.list_recordings()}
            if name not in entries:
                raise FileNotFoundError(f"Recording not found on HiDock: {name}")
            size = entries[name].size
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    sequence = self._send(5, payload)
                    saved = 0
                    while saved < size:
                        block = self._receive(5, sequence)
                        if not block or saved + len(block) > size:
                            raise ProtocolError(f"Invalid download length for {name}: expected {size} bytes")
                        output.write(block)
                        saved += len(block)
                        if progress:
                            progress(saved, size)
                    output.flush()
                    os.fsync(output.fileno())
                if overwrite:
                    os.replace(temporary, path)
                else:
                    # An atomic no-clobber publish, including concurrent writers.
                    os.link(temporary, path)
            except BaseException:
                # A failed transfer may leave queued audio; reconnect before reuse.
                self.close()
                raise
            finally:
                Path(temporary).unlink(missing_ok=True)
        if transcriber is not None:
            transcriber.transcribe(path, overwrite=overwrite)
        return path
