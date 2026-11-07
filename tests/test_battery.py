import contextlib
import io
import json
import struct
import unittest
from unittest.mock import patch

from hidock import BatteryStatus, HiDock, HiDockError, ProtocolError, TransferTimeoutError
from hidock.cli import main
from hidock.protocol import encode, parse_battery_status
from test_hidock import FakeUSB


class BatteryUSB(FakeUSB):
    def __init__(self, body=bytes.fromhex("0264003fdf90")):
        super().__init__()
        self.body = body

    def write(self, packet, timeout):
        _, command, sequence, size = struct.unpack(">HHII", packet[:12])
        if command != 0x1004:
            return super().write(packet, timeout)
        if size != 0 or len(packet) != 12:
            raise AssertionError("Battery query must have no payload")
        self.commands.append(command)
        reply = encode(command, sequence - 1, b"stale") + encode(command, sequence, self.body)
        self.queue.extend([reply[:5], reply[5:17], reply[17:]])


class BatteryTests(unittest.TestCase):
    def test_charge_states_voltage_and_percentage_boundaries(self):
        for code, state in [(0, "idle"), (1, "charging"), (2, "full"), (255, "unknown")]:
            for percent in (0, 57, 100):
                with self.subTest(code=code, percent=percent):
                    status = parse_battery_status(struct.pack(">BBI", code, percent, 4_186_000))
                    self.assertEqual(status, BatteryStatus(state, percent, 4_186_000, code))
                    self.assertAlmostEqual(status.voltage_volts, 4.186)

    def test_malformed_responses(self):
        for body in (b"", bytes(5), bytes(7), struct.pack(">BBI", 1, 101, 4_000_000)):
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                parse_battery_status(body)

    def test_api_query_matches_sequence_and_supports_connection_reuse(self):
        usb = BatteryUSB()
        with HiDock(transport=usb) as dock:
            self.assertEqual(dock.battery_status().percent, 100)
            self.assertEqual(dock.info().firmware, "1.4.7")
        self.assertEqual(usb.commands, [0x1004, 1])
        self.assertTrue(usb.closed)

    def test_api_requires_connection(self):
        with self.assertRaises(HiDockError):
            HiDock(transport=BatteryUSB()).battery_status()

    def test_api_reports_malformed_payload(self):
        with HiDock(transport=BatteryUSB(bytes(5))) as dock:
            with self.assertRaises(ProtocolError):
                dock.battery_status()

    def test_cli_text_and_json(self):
        for json_output in (False, True):
            usb = BatteryUSB()
            output = io.StringIO()
            with patch("hidock.cli.HiDock", return_value=HiDock(transport=usb)), contextlib.redirect_stdout(output):
                self.assertEqual(main(["battery"] + (["--json"] if json_output else [])), 0)
            if json_output:
                self.assertEqual(json.loads(output.getvalue()), {
                    "status": "full", "percent": 100, "voltage_microvolts": 4_186_000, "status_code": 2,
                })
            else:
                self.assertEqual(output.getvalue(), "Battery: 100% (full, 4.19 V)\n")

    def test_cli_timeout_is_clean_error(self):
        output = io.StringIO()
        with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stderr(output):
            dock.return_value.__enter__.return_value.battery_status.side_effect = TransferTimeoutError("No battery reply")
            self.assertEqual(main(["battery"]), 1)
        self.assertIn("No battery reply", output.getvalue())


if __name__ == "__main__":
    unittest.main()
