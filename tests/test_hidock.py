import struct
import tempfile
from pathlib import Path
import unittest

from hidock import HiDock, ProtocolError, TransferTimeoutError
from hidock.cli import main
from hidock.protocol import FrameReader, encode, parse_recordings


def record(name, size):
    name = name.encode() + b"\0"
    return b"\x05" + len(name).to_bytes(3, "big") + name + struct.pack(">I", size) + bytes(22)


class FakeUSB:
    def __init__(self, *, fail=False, oversize=False, callback=None):
        # Includes protocol magic in the audio; stripping by magic search corrupts it.
        self.audio = b"ID3" + encode(5, 99, b"embedded audio bytes") + b"x" * 9000
        self.queue = []
        self.commands = []
        self.closed = False
        self.fail = fail
        self.oversize = oversize
        self.callback = callback

    def open(self):
        self.closed = False

    def close(self):
        self.closed = True

    def write(self, packet, timeout):
        _, command, sequence, _ = struct.unpack(">HHII", packet[:12])
        self.commands.append(command)
        if command == 1:
            reply = encode(command, sequence, b"\0\1\4\7SERIAL")
        elif command == 6:
            reply = encode(command, sequence, struct.pack(">I", 1))
        elif command == 4:
            body = b"\xff\xff\0\0\0\1" + record("sample.hda", len(self.audio))
            # A record split across protocol frames and USB reads.
            reply = encode(command, sequence, body[:19]) + encode(command, sequence, body[19:])
        elif command == 5:
            self.asserted_filename = packet[12:]
            audio = self.audio + (b"extra" if self.oversize else b"")
            reply = encode(command, sequence, audio[:8180])
            if not self.fail:
                reply += encode(command, sequence, audio[8180:])
            if self.callback:
                self.callback()
        else:
            raise AssertionError(f"Unexpected command {command}")
        # Mix a stale reply with fragmented and coalesced protocol frames.
        reply = encode(0xB, 0, bytes(12)) + reply
        self.queue.extend([reply[:7], reply[7:31], reply[31:]])

    def read(self, timeout):
        if not self.queue:
            raise TransferTimeoutError("Simulated timeout")
        return self.queue.pop(0)


class ProtocolTests(unittest.TestCase):
    def test_split_coalesced_and_padded_frames(self):
        payload = b"audio\x12\x34data"
        padded = struct.pack(">HHII", 0x1234, 5, 3, (2 << 24) | len(payload)) + payload + b"\0\0"
        raw = padded + encode(4, 7, b"files")
        reader = FrameReader()
        frames = []
        for byte in raw:
            reader.buffer.append(byte)
            frame = reader.pop()
            if frame:
                frames.append(frame)
        self.assertEqual([(f.command, f.sequence, f.body) for f in frames],
                         [(5, 3, payload), (4, 7, b"files")])
        self.assertEqual(reader.buffer, b"")

    def test_invalid_header_and_oversized_frame(self):
        for header in (bytes(12), struct.pack(">HHII", 0x1234, 5, 1, 0xFFFFFF)):
            reader = FrameReader()
            reader.buffer.extend(header)
            with self.assertRaises(ProtocolError):
                reader.pop()

    def test_truncated_and_count_mismatch_lists_are_errors(self):
        good = b"\xff\xff\0\0\0\1" + record("sample.hda", 12345)
        self.assertEqual(parse_recordings(good)[0].size, 12345)
        for bad in (good[:-1], good[:5], good[:5] + b"\2" + good[6:], good + b"x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ProtocolError):
                    parse_recordings(bad)


class DeviceTests(unittest.TestCase):
    def test_info_list_and_stream_exact_audio(self):
        usb = FakeUSB()
        with tempfile.TemporaryDirectory() as folder, HiDock(transport=usb) as dock:
            self.assertEqual(dock.info().firmware, "1.4.7")
            recordings = dock.list_recordings()
            self.assertEqual(len(recordings), 1)
            progress = []
            path = dock.download(recordings[0], Path(folder) / "sample.hda",
                                 progress=lambda saved, total: progress.append((saved, total)))
            self.assertEqual(path.read_bytes(), usb.audio)
            self.assertEqual(progress[-1], (len(usb.audio), len(usb.audio)))
            self.assertEqual(usb.asserted_filename, b"sample.hda")
        self.assertTrue(usb.closed)
        self.assertTrue(set(usb.commands) <= {1, 4, 5, 6})

    def test_failed_download_does_not_publish_or_destroy_existing_file(self):
        for options in ({"fail": True}, {"oversize": True}):
            for existing in (False, True):
                with self.subTest(options=options, existing=existing):
                    usb = FakeUSB(**options)
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "sample.hda"
                        if existing:
                            path.write_bytes(b"keep this")
                        with HiDock(transport=usb) as dock:
                            with self.assertRaises((TransferTimeoutError, ProtocolError)):
                                dock.download("sample.hda", path, overwrite=existing)
                            self.assertTrue(usb.closed)
                        self.assertEqual(path.exists(), existing)
                        if existing:
                            self.assertEqual(path.read_bytes(), b"keep this")
                        self.assertEqual(list(Path(folder).glob("*.part")), [])

    def test_default_does_not_overwrite_even_concurrent_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.hda"
            usb = FakeUSB(callback=lambda: path.write_bytes(b"another writer"))
            with HiDock(transport=usb) as dock:
                with self.assertRaises(FileExistsError):
                    dock.download("sample.hda", path)
            self.assertEqual(path.read_bytes(), b"another writer")

    def test_existing_file_rejected_before_usb_commands(self):
        usb = FakeUSB()
        with tempfile.TemporaryDirectory() as folder, HiDock(transport=usb) as dock:
            path = Path(folder) / "sample.hda"
            path.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                dock.download("sample.hda", path)
            self.assertEqual(usb.commands, [])

    def test_unsafe_filename_rejected_before_usb_commands(self):
        usb = FakeUSB()
        with HiDock(transport=usb) as dock:
            for name in ("../escape.hda", "a/b.hda", "a\\b.hda", "a\0b.hda", ""):
                with self.assertRaises(ValueError):
                    dock.download(name, "unused.hda")
            self.assertEqual(usb.commands, [])

    def test_missing_recording(self):
        with tempfile.TemporaryDirectory() as folder, HiDock(transport=FakeUSB()) as dock:
            with self.assertRaises(FileNotFoundError):
                dock.download("missing.hda", Path(folder) / "missing.hda")
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
