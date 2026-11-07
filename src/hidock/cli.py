import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys

from . import HiDock, HiDockError, WhisperTranscriber
from .device import _filename


def _transcription_options(parser, *, download=False):
    if download:
        parser.add_argument("--transcribe", action="store_true", help="Save a local Whisper .txt transcript")
    parser.add_argument("--model", default="base", help="Whisper model name or local model directory (default: base)")
    parser.add_argument("--language", help="Language code, e.g. en (default: auto-detect)")
    parser.add_argument("--model-cache", type=Path, help="Directory for downloaded models")
    parser.add_argument("--offline", action="store_true", help="Use only already downloaded model files")


def _transcript(transcriber, path, *, overwrite=False):
    if transcriber is None:
        return
    target = path.with_suffix(".txt")
    if target.exists() and not overwrite:
        print(f"Skip transcript {target} (already exists)")
    else:
        print(transcriber.transcribe(path, overwrite=overwrite))


def main(argv=None):
    parser = argparse.ArgumentParser(description="List and download HiDock P1 recordings over USB")
    parser.add_argument("--serial", help="Select by USB serial descriptor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="Show device model, firmware, and serial")
    battery = sub.add_parser("battery", help="Show battery percentage, charge state, and voltage")
    battery.add_argument("--json", action="store_true")
    storage = sub.add_parser("storage", help="Show total, free, and used device storage")
    storage.add_argument("--json", action="store_true")
    clock = sub.add_parser("clock", help="Read or explicitly set the device clock")
    clock.add_argument("--json", action="store_true")
    setting = clock.add_mutually_exclusive_group()
    setting.add_argument("--sync", action="store_true", help="Set the clock to this computer's current local time")
    setting.add_argument("--set", dest="clock_time", type=datetime.fromisoformat,
                         metavar="ISO_TIMESTAMP", help="Set a wall-clock time, e.g. 2026-11-06T12:30:00")
    recording = sub.add_parser("recording-status", help="Show the current recording snapshot")
    recording.add_argument("--json", action="store_true")
    quality = sub.add_parser("quality", help="Read recording quality or set normal/high")
    quality.add_argument("--set", dest="quality", choices=("normal", "high"))
    quality.add_argument("--json", action="store_true")
    settings = sub.add_parser("settings", help="Read settings or change selected options")
    for option in ("auto-record", "auto-play", "notification", "bluetooth-tone", "record-on-vibe"):
        settings.add_argument(f"--{option}", choices=("on", "off"))
    settings.add_argument("--json", action="store_true")
    deletion = sub.add_parser("delete", help="Permanently delete one exact recording filename from the device")
    deletion.add_argument("name")
    deletion.add_argument("--dry-run", action="store_true", help="Check that the file exists without deleting it")
    deletion.add_argument("--json", action="store_true")
    listing = sub.add_parser("list", help="List recordings")
    listing.add_argument("--json", action="store_true")
    download = sub.add_parser("download", help="Download one recording to a file")
    download.add_argument("name")
    download.add_argument("destination", type=Path)
    download.add_argument("--overwrite", action="store_true")
    _transcription_options(download, download=True)
    sync = sub.add_parser("download-all", help="Download recordings into a directory")
    sync.add_argument("directory", type=Path)
    sync.add_argument("--overwrite", action="store_true")
    _transcription_options(sync, download=True)
    transcription = sub.add_parser("transcribe", help="Transcribe a local audio file without connecting to USB")
    transcription.add_argument("audio", type=Path)
    transcription.add_argument("--output", type=Path, help="Transcript path (default: audio filename with .txt extension)")
    transcription.add_argument("--overwrite", action="store_true")
    _transcription_options(transcription)
    args = parser.parse_args(argv)
    try:
        transcriber = None
        if args.command == "transcribe" or getattr(args, "transcribe", False):
            transcriber = WhisperTranscriber(
                model=args.model, language=args.language, cache_dir=args.model_cache,
                local_files_only=args.offline,
            )
        if args.command == "transcribe":
            print(transcriber.transcribe(args.audio, args.output, overwrite=args.overwrite))
            return 0
        with HiDock(serial=args.serial) as dock:
            if args.command == "info":
                print(json.dumps(asdict(dock.info()), indent=2))
            elif args.command == "battery":
                status = dock.battery_status()
                if args.json:
                    print(json.dumps(asdict(status), indent=2))
                else:
                    print(f"Battery: {status.percent}% ({status.status}, {status.voltage_volts:.2f} V)")
            elif args.command == "storage":
                storage = dock.storage_info()
                if args.json:
                    print(json.dumps(asdict(storage), indent=2))
                else:
                    gib = 1024 ** 3
                    print(f"Storage: {storage.used_bytes / gib:.2f} GiB used / "
                          f"{storage.total_bytes / gib:.2f} GiB total ({storage.used_percent:.1f}% used)")
                    print(f"Free: {storage.free_bytes / gib:.2f} GiB")
                    print(f"Status: 0x{storage.status_code:08x}")
            elif args.command == "clock":
                if args.sync or args.clock_time is not None:
                    value = dock.set_time(args.clock_time)
                else:
                    value = dock.get_time()
                value = value.isoformat(timespec="seconds") if value is not None else None
                if args.json:
                    print(json.dumps({"time": value}, indent=2))
                else:
                    print(f"Device clock: {value or 'unknown'} (local wall time; no timezone stored)")
            elif args.command == "recording-status":
                status = dock.recording_status()
                if args.json:
                    print(json.dumps(asdict(status), indent=2))
                elif status.is_recording:
                    print(f"Recording: {status.recording} ({status.type}, {status.duration_seconds} seconds)")
                else:
                    print("Recording: idle")
            elif args.command == "quality":
                quality = dock.set_recording_quality(args.quality) if args.quality else dock.get_recording_quality()
                if args.json:
                    print(json.dumps(asdict(quality), indent=2))
                else:
                    print(f"Recording quality: {quality.quality} (code {quality.raw_code})")
            elif args.command == "settings":
                changes = {key: getattr(args, key) == "on" for key in
                           ("auto_record", "auto_play", "notification", "bluetooth_tone", "record_on_vibe")
                           if getattr(args, key) is not None}
                settings = dock.set_settings(**changes) if changes else dock.get_settings()
                if args.json:
                    print(json.dumps(asdict(settings), indent=2))
                else:
                    for key, value in asdict(settings).items():
                        if key != "raw_values":
                            print(f"{key}: {'unknown/unavailable' if value is None else 'on' if value else 'off'}")
            elif args.command == "delete":
                _filename(args.name)
                if args.dry_run:
                    if args.name not in {r.name for r in dock.list_recordings()}:
                        raise FileNotFoundError(f"Recording not found on HiDock: {args.name}")
                    result = {"name": args.name, "deleted": False, "dry_run": True}
                    message = f"Would delete: {args.name}"
                else:
                    result = {"name": args.name, "deleted": dock.delete_recording(args.name), "dry_run": False}
                    message = f"Deleted: {args.name}"
                print(json.dumps(result, indent=2) if args.json else message)
            elif args.command == "list":
                recordings = dock.list_recordings()
                if args.json:
                    print(json.dumps([asdict(r) for r in recordings], indent=2))
                else:
                    for r in recordings:
                        print(f"{r.size:12d}  {r.name}")
                    print(f"{len(recordings)} recordings", file=sys.stderr)
            elif args.command == "download":
                path = dock.download(args.name, args.destination, overwrite=args.overwrite)
                print(path)
                _transcript(transcriber, path, overwrite=args.overwrite)
            else:
                for r in dock.list_recordings():
                    _filename(r.name)
                    target = args.directory / r.name
                    if target.is_file() and target.stat().st_size == r.size and not args.overwrite:
                        print(f"Skip {r.name} (same size)")
                        _transcript(transcriber, target)
                        continue
                    path = dock.download(r, target, overwrite=args.overwrite)
                    print(path)
                    _transcript(transcriber, path, overwrite=args.overwrite)
        return 0
    except (HiDockError, OSError, ValueError) as exc:
        print(f"hidock: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Operation interrupted", file=sys.stderr)
        return 130
