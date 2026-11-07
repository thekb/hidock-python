import contextlib
import io
import json
import struct
import unittest
from unittest.mock import patch

from hidock import HiDock, HiDockError, ProtocolError, StorageInfo, TransferTimeoutError
from hidock.cli import main
from hidock.protocol import encode, parse_storage_info
from test_hidock import FakeUSB


CAPTURED = bytes.fromhex("0000c3200000e90000000900000000024849444f434b000000000000")
MIB = 1024 * 1024


class StorageUSB(FakeUSB):
    def __init__(self, body=CAPTURED):
        super().__init__()
        self.body = body

    def write(self, packet, timeout):
        _, command, sequence, size = struct.unpack(">HHII", packet[:12])
        if command != 0x10:
            return super().write(packet, timeout)
        if size != 0 or len(packet) != 12:
            raise AssertionError("Storage query must have no payload")
        self.commands.append(command)
        reply = encode(command, sequence - 1, b"stale") + encode(command, sequence, self.body)
        self.queue.extend([reply[:5], reply[5:17], reply[17:]])


class StorageTests(unittest.TestCase):
    def test_captured_capacity_free_space_units_and_status(self):
        storage = parse_storage_info(CAPTURED)
        self.assertEqual(storage, StorageInfo(59648 * MIB, 49952 * MIB, 9696 * MIB, 0x900))
        self.assertAlmostEqual(storage.used_percent, 9696 / 59648 * 100)
        self.assertEqual(storage.free_bytes + storage.used_bytes, storage.total_bytes)

    def test_empty_and_full_storage(self):
        for free_mib, percent in [(0, 100), (65536, 0)]:
            storage = parse_storage_info(struct.pack(">III", free_mib, 65536, 0xFFFFFFFF))
            self.assertEqual(storage.used_percent, percent)
            self.assertEqual(storage.status_code, 0xFFFFFFFF)

    def test_short_and_extended_payloads(self):
        self.assertEqual(parse_storage_info(CAPTURED[:12]), parse_storage_info(CAPTURED))
        self.assertEqual(parse_storage_info(CAPTURED + b"future metadata"), parse_storage_info(CAPTURED))
        for length in (0, 4, 8, 11):
            with self.subTest(length=length), self.assertRaises(ProtocolError):
                parse_storage_info(CAPTURED[:length])

    def test_invalid_capacity_or_free_space(self):
        for free, total in [(0, 0), (0, 99), (0, 262145), (65537, 65536)]:
            with self.subTest(free=free, total=total), self.assertRaises(ProtocolError):
                parse_storage_info(struct.pack(">III", free, total, 0))

    def test_api_query_sequence_and_connection_reuse(self):
        usb = StorageUSB()
        with HiDock(transport=usb) as dock:
            self.assertEqual(dock.storage_info().total_bytes, 59648 * MIB)
            self.assertEqual(dock.info().firmware, "1.4.7")
        self.assertEqual(usb.commands, [0x10, 1])
        self.assertTrue(usb.closed)

    def test_api_requires_connection_and_reports_invalid_payload(self):
        with self.assertRaises(HiDockError):
            HiDock(transport=StorageUSB()).storage_info()
        with HiDock(transport=StorageUSB(bytes(8))) as dock:
            with self.assertRaises(ProtocolError):
                dock.storage_info()

    def test_cli_text_and_json(self):
        for json_output in (False, True):
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=StorageUSB())), contextlib.redirect_stdout(output):
                self.assertEqual(main(["storage"] + (["--json"] if json_output else [])), 0)
            if json_output:
                self.assertEqual(json.loads(output.getvalue()), {
                    "total_bytes": 59648 * MIB, "free_bytes": 49952 * MIB,
                    "used_bytes": 9696 * MIB, "status_code": 0x900,
                })
            else:
                self.assertEqual(output.getvalue(),
                                 "Storage: 9.47 GiB used / 58.25 GiB total (16.3% used)\n"
                                 "Free: 48.78 GiB\nStatus: 0x00000900\n")

    def test_cli_timeout_is_clean_error(self):
        output = io.StringIO()
        with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(output):
            dock.return_value.__enter__.return_value.storage_info.side_effect = TransferTimeoutError("No storage reply")
            self.assertEqual(main(["storage"]), 1)
        self.assertIn("No storage reply", output.getvalue())


if __name__ == "__main__":
    unittest.main()
