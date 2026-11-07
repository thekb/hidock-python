# hidock-python

A Python library and command line tool to list and download recordings directly
from a HiDock P1 over USB. No HiNotes account or cloud service is needed.

Hardware verified on a P1 (`10d6:b00e`), firmware 1.4.7, using macOS on Apple
Silicon. H1, H1E, and P1 Mini are not enabled or tested. The library supports
device information, battery/storage status, clock, recording status/quality,
settings, listing, downloads, and individual recording deletion. Writes and
deletion occur only through explicit API calls or CLI options.

## Install

Python 3.10 or newer and a native libusb installation are required.

```sh
# macOS
brew install libusb

python3 -m venv .venv
source .venv/bin/activate
python -m pip install hidock-python
```

The PyPI distribution is `hidock-python`; Python imports remain `hidock` and
the command line executable is `hidock`. To work from a source checkout, use
`python -m pip install -e .` instead.

On Debian/Ubuntu, install `libusb-1.0-0` and grant your user access to the P1
with an appropriate udev rule. Windows requires libusb and a compatible USB
driver for the vendor interface. These platforms have not been hardware tested.

Disconnect the recorder in HiNotes or close the browser tab using it before
connecting through Python. One application can claim its USB interface at a time.
Use the USB data connection on the recorder.

## Command line

```sh
hidock info
hidock battery
hidock battery --json
hidock storage
hidock storage --json
hidock clock
hidock clock --sync
hidock recording-status
hidock recording-status --json
hidock quality
hidock settings
hidock list
hidock list --json
hidock download '2026Jan01-120000-Rec00.hda' recordings/sample.hda
hidock download-all recordings/
```

`python -m hidock` accepts the same arguments. `--serial USB_SERIAL` before
the subcommand selects a device by its USB serial descriptor (which can differ
from the serial returned by `info`).

`download` takes an exact device filename and an output **file path**.
`download-all` takes an output directory, preserves device filenames, and skips
existing files with the same byte count. A same-size skip does not verify file
contents. If an existing file has a different size, the command stops to avoid
overwriting it. Use `--overwrite` explicitly to replace local files.

## Python

```python
from pathlib import Path
from hidock import HiDock

with HiDock() as dock:
    print(dock.info())
    recordings = dock.list_recordings()
    for recording in recordings:
        print(recording.name, recording.size)

    if recordings:
        recording = recordings[0]
        dock.download(recording, Path('recordings') / recording.name)
```

You can also pass an exact filename to `download`. Its optional
`progress(bytes_saved, total_bytes)` callback runs synchronously during transfer.
Set `timeout=20` on `HiDock` to allow a longer idle wait per response frame.
Keep one device session for sequential operations; operations on an instance
are serialized. A failed download closes the connection so queued data cannot
contaminate a retry; open a new context before retrying.

Each `Recording` has `name`, `size`, `version`, and the device's opaque
`signature` metadata. The signature is not treated as a content checksum.

## Battery status

```python
from hidock import HiDock

with HiDock() as dock:
    battery = dock.battery_status()
    print(battery.percent, battery.status, battery.voltage_volts)
```

`battery_status()` returns a `BatteryStatus` with `percent` (0–100), `status`
(`idle`, `charging`, `full`, or `unknown`), `voltage_microvolts`, and the raw
`status_code`. The `voltage_volts` property converts microvolts to volts.
Unrecognized state codes are preserved and labeled `unknown`; malformed
responses and percentages above 100 raise `ProtocolError`.

The read-only battery command (`0x1004`) was verified on the connected P1
with firmware 1.4.7. `hidock battery` prints a short status line;
`hidock battery --json` outputs the four stored fields for scripts.

## Storage information

```python
from hidock import HiDock

with HiDock() as dock:
    storage = dock.storage_info()
    print(storage.total_bytes, storage.free_bytes, storage.used_bytes)
    print(storage.used_percent)
```

`storage_info()` returns a `StorageInfo` with `total_bytes`, `free_bytes`,
`used_bytes`, and the raw card `status_code`. Its `used_percent` property gives
the percentage of capacity used. `hidock storage` displays sizes in GiB;
`hidock storage --json` outputs the four stored fields, with sizes in bytes.

The read-only card-information command (`0x0010`) was verified on the P1 with
firmware 1.4.7. The device reports free and total capacity in whole MiB; byte
values therefore have MiB precision. Used space is total minus free, so it
includes filesystem overhead and can differ from the sum of recording sizes.
Usable capacity can be smaller than the advertised storage size. The raw status
code is preserved without interpreting it as a health assessment.

Truncated replies, capacities outside the supported 100 MiB–256 GiB range,
and free space greater than capacity raise `ProtocolError`. Extra firmware
metadata following the first 12 payload bytes is ignored.

## Device clock and recording status

```python
from datetime import datetime
from hidock import HiDock

with HiDock() as dock:
    print(dock.get_time())         # datetime, or None if uninitialized
    print(dock.recording_status())
    # Explicitly change the clock, if desired:
    dock.set_time()               # synchronize to the computer's local time
    # dock.set_time(datetime(2026, 11, 6, 12, 30))
```

`hidock clock` reads the clock. `hidock clock --sync` synchronizes it with the
computer's current local time; `hidock clock --set 2026-11-06T12:30:00` supplies
a specific time. Both read and write commands accept `--json` and output
`{"time": "2026-11-06T12:30:00"}` (or `null` for an uninitialized clock).
Clock reads do not modify the device. The clock stores wall time with one-second
precision and no timezone. Timezone-aware Python values or ISO inputs retain
their supplied wall-clock components without conversion. A successful set
returns the requested time after checking the device acknowledgement; call
`get_time()` separately for readback. Malformed clock data and rejected clock
updates raise `ProtocolError`.

`recording_status()` returns `RecordingStatus` with `is_recording`, `recording`
(the active filename or `None`), `duration_seconds`, `type` (`recording`,
`whisper`, `unknown`, or `None`), `type_code`, and `samples` (raw sample values).
An empty response indicates idle. Unknown recording type codes are preserved.
The CLI command `hidock recording-status` displays the current snapshot;
`--json` includes all fields. It never starts or stops recording. Duration is
the device's 16-bit field, and the library does not infer longer elapsed times.

Clock reading (`0x0002`) and an idle status reply (`0x001d`) were verified on
the P1 with firmware 1.4.7. Clock writing (`0x0003`) and active recording payloads
follow the public HiNotes parser and are covered by simulated-device tests;
they have not been exercised against a live recording on this device.

## Recording quality, settings, and file management

```sh
hidock quality
hidock quality --set high
hidock quality --set normal
hidock settings --json
hidock settings --auto-record off --auto-play on
hidock settings --notification off --bluetooth-tone on --record-on-vibe on
hidock delete 'exact-recording-name.hda' --dry-run
hidock delete 'exact-recording-name.hda'
```

`quality` and `settings` read the current values when no write options are
provided. Both support `--json`. Supported settings are `auto_record`,
`auto_play`, `notification` (notification sounds), `bluetooth_tone` (Bluetooth
prompt sounds), and `record_on_vibe` (record during Vibe Mic use).

```python
from hidock import HiDock

with HiDock() as dock:
    quality = dock.get_recording_quality()  # RecordingQuality(quality, raw_code)
    settings = dock.get_settings()         # DeviceSettings
    # Explicit writes, when desired:
    # dock.set_recording_quality("high")
    # dock.set_settings(auto_play=True, notification=False)
    # dock.delete_recording("exact-recording-name.hda")
```

Recording quality is `normal`, `high`, or `unknown` with the raw numeric code
preserved. For normal quality writes, the P1's firmware-dependent encoding is
selected automatically: code 2 before 1.4.6, code 0 from 1.4.6 onward. High uses
code 1. The setting affects future captures, not previously saved audio.
Changing quality does not convert existing downloads.

`get_settings()` returns a `DeviceSettings` object containing five optional
boolean fields and `raw_values` (the original uint32 fields). Missing or
unrecognized setting values are `None`. `set_settings()` changes only supplied
boolean fields; `None` means leave unchanged. Unsupported fields and invalid
values are rejected before sending a write. Omitted fields use the protocol's
zero/unchanged sentinel, including for Bluetooth tones, whose on/off encoding
is inverted. Quality and settings writes check the acknowledgement, then read
back the result. A mismatch raises `ProtocolError`; it does not automatically
roll back an acknowledged change. After a timeout, re-read the current state
before deciding to retry.

File management includes the existing `list`, `download`, and `download-all`
commands plus `delete` for permanent deletion of a single exact device filename.
There is no wildcard expansion inside the library. `delete --dry-run` checks
the file exists and displays the intended deletion without sending a delete
command. Actual deletion rechecks the file list and current recording status,
refuses an active recording, and checks the device acknowledgement. It leaves
local downloaded audio and transcripts untouched. The device can change state
between the status check and deletion; the library cannot make those commands
atomic with physical button presses or other device activity.

`delete_recording(name_or_recording)` returns `True` after a successful deletion.
Missing files raise `FileNotFoundError`; use `missing_ok=True` to return `False`
instead. Failed or malformed write acknowledgements raise `ProtocolError`.

Quality and settings reads were verified on the P1 with firmware 1.4.7. Write
payloads follow the public HiNotes implementation. Writes and deletion are
covered by simulated-device tests; no recordings were deleted or device
settings changed during hardware verification. Storage formatting, firmware
updates, file renaming, and file uploads are not implemented.

## Local transcription

Install the optional transcription dependencies:

```sh
python -m pip install 'hidock-python[transcription]'
```

From a source checkout, use `python -m pip install -e '.[transcription]'`.

Add `--transcribe` to either download command to save a UTF-8 `.txt` transcript
beside each audio file (`sample.hda` produces `sample.txt`):

```sh
hidock download '2026Jan01-120000-Rec00.hda' recordings/sample.hda --transcribe
hidock download-all recordings/ --transcribe --language en
hidock transcribe recordings/sample.hda --model base --language en
```

The standalone `transcribe` command works on local audio without a USB
connection. Use `--output transcript.txt` to choose a different transcript path.
The `--model`, `--language`, `--model-cache`, and `--offline` options are supported
by both download commands and by `transcribe`.

Speech recognition runs locally using
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), on the CPU with int8
computation. Audio is never uploaded. The default multilingual `base` model
downloads from Hugging Face on first transcription and is reused across files.
Set `--model tiny` for a smaller model or `--model small` for a larger one.
Language is detected automatically unless supplied with `--language en` (or
another language code). Model accuracy varies with speech and recording quality.
Silence detection is enabled; recordings without detected speech produce an
empty transcript. There is no speaker identification.

Use `--model-cache .models` to keep model downloads in a chosen directory.
After downloading once, `--offline` restricts loading to cached files; `--model`
can also name a local converted Whisper model directory. No external FFmpeg
installation is needed: faster-whisper uses PyAV to decode the audio, including
the P1's MP3 content in `.hda` files.

`download-all --transcribe` also transcribes existing same-size audio files
whose transcripts are missing. Existing transcripts are skipped unless
`--overwrite` is set. `--overwrite` on download commands replaces both local
audio and transcripts. A transcription error stops the command with a nonzero
exit code and retains completed audio and previous transcripts. Retry with
`hidock transcribe`, or rerun the batch to fill missing transcripts.

The Python API supports the same flow, with one reusable model:

```python
from hidock import HiDock, WhisperTranscriber

transcriber = WhisperTranscriber(model="base", language="en")
with HiDock() as dock:
    recordings = dock.list_recordings()
    if recordings:
        dock.download(recordings[0], "recordings/sample.hda", transcriber=transcriber)

# Or transcribe previously downloaded audio without opening the device:
transcriber.transcribe("recordings/other.hda")
```

`WhisperTranscriber.transcribe` returns the transcript path, and refuses to
replace an existing transcript unless `overwrite=True`. `HiDock.download`
continues to return the audio path. You may implement the `Transcriber` protocol
to plug in another engine. Base USB functionality does not import or require
faster-whisper when transcription is unused.

## File handling and validation

Downloads stream to a temporary file beside the requested destination and are
published only when their byte count matches the device's file-list metadata.
An interrupted or short transfer raises an error and removes its temporary
file. Existing files are preserved by default, including when another process
creates the destination during a download.

USB protocol frames are decoded by declared payload length, independent of USB
read boundaries. Every frame header and padding is removed without searching
audio bytes for header-like patterns. Memory use during transfer is bounded
by USB/frame buffers rather than recording length.

Original recording bytes are preserved. The small `.hda` sample downloaded from
the tested P1 is MP3 audio (48 kHz mono, 96 kbps). You may choose an `.mp3`
destination for compatible recordings; changing the extension does not convert
audio. The library does not assume every firmware or recording mode uses MP3.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests exercise fragmented and coalesced frames, padding, embedded header-like
audio bytes, multi-frame file lists and downloads, malformed responses,
timeouts, atomic publication, and overwrite protection without hardware.

## Protocol references

MIT licensed; see [LICENSE](https://github.com/thekb/hidock-python/blob/main/LICENSE).
Maintainers can follow the [release guide](https://github.com/thekb/hidock-python/blob/main/docs/releasing.md)
to build and publish packages.

The implementation was independently written using the public
[hidock-mcp protocol implementation](https://github.com/kms254/hidock-mcp/blob/main/src/protocol.ts)
and [HiDock Local P1 protocol notes](https://github.com/andre-wiedemann/hidock-local/blob/main/docs/USB_PROTOCOL.md),
then checked against the connected P1. Some historical P1 notes use hard-coded
sequence numbers and approximate download sizes; this implementation uses
incrementing big-endian sequence numbers, response frame lengths, and exact
file-list byte counts, as observed on the tested device.
