import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hidock import HiDock, TranscriptionError, WhisperTranscriber
from hidock.cli import main
from hidock.protocol import Recording
from test_hidock import FakeUSB


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.audio = Path(self.folder.name) / "sample.hda"
        self.audio.write_bytes(b"audio")
        self.model = Mock()
        self.model.transcribe.side_effect = lambda *a, **kw: (
            iter([SimpleNamespace(text=" Hello world. "), SimpleNamespace(text="  ")]), SimpleNamespace(duration=2))
        self.factory = Mock(return_value=self.model)
        self.modules = patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=self.factory)})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_local_model_is_reused_and_text_saved(self):
        engine = WhisperTranscriber("tiny", language="en", cache_dir=self.folder.name, local_files_only=True)
        target = engine.transcribe(self.audio)
        self.assertEqual(target.read_text(), "Hello world.\n")
        engine.transcribe(self.audio, overwrite=True)
        self.factory.assert_called_once_with("tiny", device="cpu", compute_type="int8",
                                             download_root=self.folder.name, local_files_only=True)
        self.model.transcribe.assert_called_with(str(self.audio), language="en", vad_filter=True)

    def test_generator_failure_keeps_audio_and_previous_transcript(self):
        def failing_segments():
            yield SimpleNamespace(text="partial text")
            raise RuntimeError("inference failed")
        self.model.transcribe.side_effect = lambda *a, **kw: (failing_segments(), SimpleNamespace(duration=2))
        target = self.audio.with_suffix(".txt")
        target.write_text("previous transcript")
        with self.assertRaisesRegex(TranscriptionError, "audio retained"):
            WhisperTranscriber().transcribe(self.audio, overwrite=True)
        self.assertEqual(target.read_text(), "previous transcript")
        self.assertEqual(self.audio.read_bytes(), b"audio")
        self.assertEqual(list(self.audio.parent.glob("*.part")), [])

    def test_default_preserves_existing_transcript_and_source(self):
        engine = WhisperTranscriber()
        self.audio.with_suffix(".txt").write_text("existing")
        with self.assertRaises(FileExistsError):
            engine.transcribe(self.audio)
        for target in (self.audio, self.audio.parent / "alias.hda"):
            if target != self.audio:
                os.link(self.audio, target)
            with self.assertRaises(ValueError):
                engine.transcribe(self.audio, target, overwrite=True)
        self.factory.assert_not_called()
        self.assertEqual(self.audio.read_bytes(), b"audio")

    def test_no_speech_produces_empty_transcript(self):
        self.model.transcribe.side_effect = lambda *a, **kw: (iter([]), SimpleNamespace(duration=2))
        path = WhisperTranscriber().transcribe(self.audio)
        self.assertEqual(path.read_text(), "")

    def test_empty_decoded_audio_is_an_error(self):
        self.model.transcribe.side_effect = lambda *a, **kw: (iter([]), SimpleNamespace(duration=0))
        with self.assertRaisesRegex(TranscriptionError, "no decoded samples"):
            WhisperTranscriber().transcribe(self.audio)
        self.assertFalse(self.audio.with_suffix(".txt").exists())

    def test_missing_optional_dependency_has_install_hint(self):
        with patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertRaisesRegex(TranscriptionError, "transcription"):
                WhisperTranscriber()

    def test_download_can_transcribe_and_retains_audio_on_failure(self):
        engine = Mock()
        engine.transcribe.side_effect = TranscriptionError("inference failed")
        with HiDock(transport=FakeUSB()) as dock:
            with self.assertRaises(TranscriptionError):
                dock.download("sample.hda", self.audio, overwrite=True, transcriber=engine)
            self.assertEqual(self.audio.read_bytes(), dock._transport.audio)
            self.assertFalse(dock._transport.closed)
        engine.transcribe.assert_called_once_with(self.audio, overwrite=True)

    def test_cli_local_transcription_never_opens_usb(self):
        with patch("hidock.cli.HiDock") as dock, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["transcribe", str(self.audio), "--model", "tiny", "--language", "en"]), 0)
        dock.assert_not_called()
        self.assertEqual(self.audio.with_suffix(".txt").read_text(), "Hello world.\n")

    def test_batch_transcribes_skipped_audio_and_skips_existing_transcript(self):
        dock = Mock()
        dock.list_recordings.return_value = [Recording(self.audio.name, self.audio.stat().st_size, 5, "")]
        with patch("hidock.cli.HiDock") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.__enter__.return_value = dock
            args = ["download-all", self.folder.name, "--transcribe"]
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 0)
        dock.download.assert_not_called()
        self.assertEqual(self.model.transcribe.call_count, 1)
        self.assertEqual(self.audio.with_suffix(".txt").read_text(), "Hello world.\n")

    def test_cli_single_download_transcribes_saved_file(self):
        dock = Mock()
        dock.download.return_value = self.audio
        with patch("hidock.cli.HiDock") as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.__enter__.return_value = dock
            self.assertEqual(main(["download", "sample.hda", str(self.audio), "--transcribe"]), 0)
        dock.download.assert_called_once_with("sample.hda", self.audio, overwrite=False)
        self.assertEqual(self.audio.with_suffix(".txt").read_text(), "Hello world.\n")


if __name__ == "__main__":
    unittest.main()
