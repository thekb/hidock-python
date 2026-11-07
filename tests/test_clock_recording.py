import contextlib
from datetime import datetime, timezone
import io
import json
import struct
import unittest
from unittest.mock import patch

from hidock import HiDock, HiDockError, ProtocolError, TransferTimeoutError
from hidock.cli import main
from hidock.protocol import (encode, encode_device_time, parse_device_time,
                             parse_recording_status)
from test_hidock import FakeUSB


def active_status(name="sample.hda", kind=0, duration=321, samples=b"\x01\x80\xff"):
    name = name.encode()
    return bytes([kind, len(name)]) + name + struct.pack(">HB", duration, len(samples)) + samples


class ClockUSB(FakeUSB):
    def __init__(self, *, status=b"", acknowledgement=b"\0"):
        super().__init__()
        self.clock = bytes.fromhex("20261106225715")
        self.status = status
        self.acknowledgement = acknowledgement
        self.writes = []

    def write(self, packet, timeout):
        _, command, sequence, length = struct.unpack(">HHII", packet[:12])
        self.writes.append(packet)
        self.commands.append(command)
        payload = packet[12:]
        if len(payload) != length:
            raise AssertionError("Incorrect outgoing payload length")
        if command == 2:
            if payload:
                raise AssertionError("Clock read must have no payload")
            body = self.clock
        elif command == 3:
            if len(payload) != 7:
                raise AssertionError("Set time requires 7 bytes")
            if self.acknowledgement == b"\0":
                self.clock = payload
            body = self.acknowledgement
        elif command == 0x1D:
            if payload:
                raise AssertionError("Recording query must have no payload")
            body = self.status
        else:
            raise AssertionError(f"Unexpected command {command}")
        reply = encode(command, sequence - 1, b"stale") + encode(command, sequence, body)
        self.queue.extend([reply[:7], reply[7:19], reply[19:]])


class ClockTests(unittest.TestCase):
    def test_bcd_roundtrip_including_leap_day_and_small_year(self):
        for time in [datetime(2024, 2, 29, 23, 59, 59), datetime(1, 1, 1), datetime(9999, 12, 31)]:
            self.assertEqual(parse_device_time(encode_device_time(time)), time)
        self.assertEqual(encode_device_time(datetime(2026, 11, 6, 22, 57, 15)), bytes.fromhex("20261106225715"))

    def test_unknown_and_malformed_time(self):
        self.assertIsNone(parse_device_time(bytes(7)))
        for body in [bytes(6), bytes(8), bytes.fromhex("20261a06225715"), bytes.fromhex("20260230225715")]:
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                parse_device_time(body)

    def test_get_set_and_readback(self):
        usb = ClockUSB()
        with HiDock(transport=usb) as dock:
            self.assertEqual(dock.get_time(), datetime(2026, 11, 6, 22, 57, 15))
            value = datetime(2024, 2, 29, 12, 34, 56, 1234, tzinfo=timezone.utc)
            self.assertEqual(dock.set_time(value), value.replace(tzinfo=None, microsecond=0))
            self.assertEqual(dock.get_time(), value.replace(tzinfo=None, microsecond=0))
        self.assertEqual(usb.commands, [2, 3, 2])

    def test_sync_uses_computer_local_time(self):
        now = datetime(2026, 11, 6, 14, 10, 20, 900)
        with patch("hidock.device.datetime") as clock, HiDock(transport=ClockUSB()) as dock:
            clock.now.return_value = now
            self.assertEqual(dock.set_time(), now.replace(microsecond=0))

    def test_rejected_and_malformed_acknowledgements(self):
        for ack in [b"", b"\1", b"\0\0"]:
            with HiDock(transport=ClockUSB(acknowledgement=ack)) as dock:
                with self.assertRaises(ProtocolError):
                    dock.set_time(datetime(2026, 1, 1))

    def test_cli_read_and_explicit_set(self):
        for extra, commands, expected in [([], [2], "2026-11-06T22:57:15"),
                                          (["--set", "2024-02-29T12:30:00"], [3], "2024-02-29T12:30:00")]:
            usb = ClockUSB()
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=usb)), contextlib.redirect_stdout(output):
                self.assertEqual(main(["clock", "--json"] + extra), 0)
            self.assertEqual(json.loads(output.getvalue()), {"time": expected})
            self.assertEqual(usb.commands, commands)

    def test_cli_sync_and_set_mutually_exclusive_and_invalid_date(self):
        for args in [["clock", "--sync", "--set", "2026-01-01"], ["clock", "--set", "2026-02-30"]]:
            with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(args)
                dock.assert_not_called()


class RecordingStatusTests(unittest.TestCase):
    def test_idle_and_active_status_fields(self):
        status = parse_recording_status(b"")
        self.assertFalse(status.is_recording)
        self.assertIsNone(status.recording)
        for kind, label in [(0, "recording"), (1, "whisper"), (255, "unknown")]:
            with self.subTest(kind=kind):
                status = parse_recording_status(active_status(kind=kind))
                self.assertTrue(status.is_recording)
                self.assertEqual((status.recording, status.duration_seconds), ("sample.hda", 321))
                self.assertEqual((status.type, status.type_code), (label, kind))
                self.assertEqual(status.samples, (1, 128, 255))

    def test_duration_boundary_and_no_samples(self):
        status = parse_recording_status(active_status(duration=65535, samples=b""))
        self.assertEqual(status.duration_seconds, 65535)
        self.assertEqual(status.samples, ())

    def test_malformed_payloads(self):
        valid = active_status()
        for body in [b"\0", valid[:4], valid[:-1], valid + b"\0", b"\0\x01\xff\0\0\0"]:
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                parse_recording_status(body)

    def test_api_status_and_clock_reuse(self):
        usb = ClockUSB(status=active_status())
        with HiDock(transport=usb) as dock:
            self.assertTrue(dock.recording_status().is_recording)
            self.assertIsNotNone(dock.get_time())
        self.assertEqual(usb.commands, [0x1D, 2])

    def test_cli_idle_and_active_json(self):
        for body, json_output in [(b"", False), (active_status(), True)]:
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=ClockUSB(status=body))), contextlib.redirect_stdout(output):
                self.assertEqual(main(["recording-status"] + (["--json"] if json_output else [])), 0)
            if json_output:
                result = json.loads(output.getvalue())
                self.assertEqual(result["samples"], [1, 128, 255])
                self.assertTrue(result["is_recording"])
            else:
                self.assertEqual(output.getvalue(), "Recording: idle\n")

    def test_not_connected_and_cli_timeout(self):
        with self.assertRaises(HiDockError):
            HiDock(transport=ClockUSB()).recording_status()
        with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(io.StringIO()):
            dock.return_value.__enter__.return_value.recording_status.side_effect = TransferTimeoutError("no reply")
            self.assertEqual(main(["recording-status"]), 1)


if __name__ == "__main__":
    unittest.main()
