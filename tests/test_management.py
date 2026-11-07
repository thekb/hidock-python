import contextlib
import io
import json
import struct
import unittest
from unittest.mock import patch

from hidock import HiDock, HiDockError, ProtocolError, TransferTimeoutError
from hidock.cli import main
from hidock.protocol import (encode, encode_settings, parse_device_settings,
                             parse_recording_quality)
from test_clock_recording import active_status
from test_hidock import FakeUSB


class ManagementUSB(FakeUSB):
    def __init__(self, *, ack=b"\0", apply=True, firmware=7, settings=(1, 2, 2, 2, 1), active=False):
        super().__init__()
        self.ack = ack
        self.apply = apply
        self.firmware = firmware
        self.settings = list(settings)
        self.quality = 2
        self.active = active
        self.deleted = False
        self.writes = []

    def write(self, packet, timeout):
        _, command, sequence, length = struct.unpack(">HHII", packet[:12])
        payload = packet[12:]
        if len(payload) != length:
            raise AssertionError("Incorrect request payload size")
        self.writes.append((command, payload))
        if command not in (1, 7, 0x0B, 0x0C, 0x1D, 0x1E, 0x1F):
            return super().write(packet, timeout)
        self.commands.append(command)
        if command == 1:
            body = bytes([0, 1, 4, self.firmware]) + b"SERIAL"
        elif command == 0x0B:
            body = b"".join(code.to_bytes(4, "big") for code in self.settings)
        elif command == 0x1F:
            body = self.quality.to_bytes(4, "big")
        elif command == 0x1D:
            body = active_status() if self.active else b""
        else:
            body = self.ack
            if body == b"\0" and self.apply:
                if command == 0x1E:
                    self.quality = int.from_bytes(payload, "big")
                elif command == 0x0C:
                    for i in range(0, len(payload), 4):
                        code = int.from_bytes(payload[i:i + 4], "big")
                        if code:
                            self.settings[i // 4] = code
                elif command == 7:
                    if payload != b"sample.hda":
                        raise AssertionError("Unexpected filename sent for deletion")
                    self.deleted = True
        reply = encode(command, sequence - 1, b"old") + encode(command, sequence, body)
        self.queue.extend([reply[:5], reply[5:17], reply[17:]])


class ManagementTests(unittest.TestCase):
    def test_quality_parsing_codes_and_malformed(self):
        for code, label in [(0, "normal"), (1, "high"), (2, "normal"), (99, "unknown")]:
            parsed = parse_recording_quality(code.to_bytes(4, "big"))
            self.assertEqual((parsed.quality, parsed.raw_code), (label, code))
        for body in (b"", bytes(3), bytes(5)):
            with self.assertRaises(ProtocolError):
                parse_recording_quality(body)

    def test_quality_set_firmware_encoding_and_readback(self):
        for firmware, value, code in [(5, "normal", 2), (6, "normal", 0), (7, "normal", 0), (7, "high", 1)]:
            usb = ManagementUSB(firmware=firmware)
            with HiDock(transport=usb) as dock:
                self.assertEqual(dock.set_recording_quality(value).quality, value)
            self.assertIn((0x1E, code.to_bytes(4, "big")), usb.writes)

    def test_quality_rejects_invalid_input_and_failed_readback(self):
        usb = ManagementUSB(apply=False)
        with HiDock(transport=usb) as dock:
            with self.assertRaises(ValueError):
                dock.set_recording_quality("maximum")
            self.assertEqual(usb.commands, [])
            with self.assertRaisesRegex(ProtocolError, "readback differs"):
                dock.set_recording_quality("high")

    def test_settings_decode_optional_and_unknown_fields(self):
        body = bytes.fromhex("0000000100000002000000020000000200000001")
        settings = parse_device_settings(body)
        self.assertTrue(settings.auto_record)
        self.assertFalse(settings.auto_play)
        self.assertFalse(settings.notification)
        self.assertTrue(settings.bluetooth_tone)
        self.assertTrue(settings.record_on_vibe)
        self.assertEqual(settings.raw_values, (1, 2, 2, 2, 1))
        old = parse_device_settings(body[:8])
        self.assertIsNone(old.record_on_vibe)
        self.assertIsNone(parse_device_settings(bytes(8)).auto_record)
        for bad in (b"", bytes(7), bytes(9)):
            with self.assertRaises(ProtocolError):
                parse_device_settings(bad)

    def test_patch_encoding_preserves_omitted_fields_and_inverts_bluetooth(self):
        self.assertEqual(encode_settings({"auto_record": False}), bytes.fromhex("00000002"))
        self.assertEqual(encode_settings({"bluetooth_tone": True}), bytes(12) + bytes.fromhex("00000002"))
        usb = ManagementUSB()
        with HiDock(transport=usb) as dock:
            result = dock.set_settings(auto_play=True, bluetooth_tone=False)
        self.assertTrue(result.auto_play)
        self.assertFalse(result.bluetooth_tone)
        self.assertEqual(usb.settings, [1, 1, 2, 1, 1])
        self.assertEqual([p for c, p in usb.writes if c == 0x0C],
                         [bytes.fromhex("00000000000000010000000000000001")])

    def test_settings_invalid_or_unavailable_changes_send_no_write(self):
        usb = ManagementUSB(settings=(1, 2))
        with HiDock(transport=usb) as dock:
            for changes in ({}, {"auto_record": 1}, {"auto_play": "off"}):
                with self.assertRaises(ValueError):
                    dock.set_settings(**changes)
            with self.assertRaises(HiDockError):
                dock.set_settings(record_on_vibe=True)
        self.assertNotIn(0x0C, usb.commands)
        with self.assertRaises(ValueError):
            encode_settings({"unknown": True})

    def test_settings_readback_mismatch(self):
        with HiDock(transport=ManagementUSB(apply=False)) as dock:
            with self.assertRaisesRegex(ProtocolError, "readback differs"):
                dock.set_settings(auto_record=False)

    def test_write_acknowledgement_errors(self):
        for ack in (b"", b"\2", b"\0\0"):
            for operation in (lambda d: d.set_recording_quality("high"),
                              lambda d: d.set_settings(auto_record=False),
                              lambda d: d.delete_recording("sample.hda")):
                with HiDock(transport=ManagementUSB(ack=ack)) as dock:
                    with self.assertRaises(ProtocolError):
                        operation(dock)

    def test_delete_exact_name_and_active_recording_protection(self):
        usb = ManagementUSB()
        with HiDock(transport=usb) as dock:
            self.assertTrue(dock.delete_recording("sample.hda"))
        self.assertTrue(usb.deleted)
        self.assertIn((7, b"sample.hda"), usb.writes)
        usb = ManagementUSB(active=True)
        with HiDock(transport=usb) as dock:
            with self.assertRaisesRegex(HiDockError, "active recording"):
                dock.delete_recording("sample.hda")
        self.assertNotIn(7, usb.commands)

    def test_missing_invalid_and_disappeared_files(self):
        usb = ManagementUSB()
        with HiDock(transport=usb) as dock:
            for name in ("../sample.hda", "a\0b", ""):
                with self.assertRaises(ValueError):
                    dock.delete_recording(name)
            self.assertEqual(usb.commands, [])
            with self.assertRaises(FileNotFoundError):
                dock.delete_recording("missing.hda")
            self.assertFalse(dock.delete_recording("missing.hda", missing_ok=True))
        self.assertNotIn(7, usb.commands)
        with HiDock(transport=ManagementUSB(ack=b"\1")) as dock:
            with self.assertRaises(FileNotFoundError):
                dock.delete_recording("sample.hda")
            self.assertFalse(dock.delete_recording("sample.hda", missing_ok=True))

    def test_cli_read_commands_do_not_write(self):
        for command, field in [("quality", "quality"), ("settings", "auto_record")]:
            usb = ManagementUSB()
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=usb)), contextlib.redirect_stdout(output):
                self.assertEqual(main([command, "--json"]), 0)
            self.assertIn(field, json.loads(output.getvalue()))
            self.assertFalse(set(usb.commands) & {7, 0x0C, 0x1E})

    def test_cli_explicit_settings_and_quality_updates(self):
        for args, command in [(["settings", "--auto-play", "on"], 0x0C), (["quality", "--set", "high"], 0x1E)]:
            usb = ManagementUSB()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=usb)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertIn(command, usb.commands)

    def test_cli_delete_dry_run_and_explicit_delete(self):
        for dry_run in (True, False):
            usb = ManagementUSB()
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=usb)), contextlib.redirect_stdout(output):
                self.assertEqual(main(["delete", "sample.hda", "--json"] + (["--dry-run"] if dry_run else [])), 0)
            self.assertEqual(json.loads(output.getvalue()), {"name": "sample.hda", "deleted": not dry_run, "dry_run": dry_run})
            self.assertEqual(7 in usb.commands, not dry_run)

    def test_cli_reports_device_failure_and_invalid_choices(self):
        with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(io.StringIO()):
            dock.return_value.__enter__.return_value.get_settings.side_effect = TransferTimeoutError("no reply")
            self.assertEqual(main(["settings"]), 1)
        for args in (["quality", "--set", "bad"], ["settings", "--auto-play", "maybe"]):
            with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(args)
                dock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
