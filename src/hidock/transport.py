"""PyUSB transport; claim only the recorder's vendor interface."""

import ctypes.util
from pathlib import Path
import time

import usb.backend.libusb1
import usb.core
import usb.util

from .errors import DeviceBusyError, DeviceNotFoundError, HiDockError, TransferTimeoutError

VENDOR_ID = 0x10D6
PRODUCT_ID = 0xB00E  # P1; other models require hardware verification.


def _library(name):
    found = ctypes.util.find_library(name)
    if found:
        return found
    for path in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
        if Path(path).exists():
            return path
    return None


class USBTransport:
    def __init__(self, *, serial: str | None = None):
        self.serial = serial
        self.device = None

    def open(self):
        backend = usb.backend.libusb1.get_backend(find_library=_library)
        if backend is None:
            raise HiDockError("libusb not found. On macOS install it with: brew install libusb")
        device = None
        try:
            devices = list(usb.core.find(find_all=True, idVendor=VENDOR_ID,
                                        idProduct=PRODUCT_ID, backend=backend))
            if self.serial is not None:
                devices = [d for d in devices if d.serial_number == self.serial]
            if not devices:
                raise DeviceNotFoundError("No HiDock P1 found. Check the USB data cable and USB access permissions.")
            if len(devices) > 1:
                raise HiDockError("Multiple P1 devices found; select one using serial= or --serial.")
            device = devices[0]
            # Avoid resetting an already configured device.
            if device.get_active_configuration().bConfigurationValue != 1:
                device.set_configuration(1)
            usb.util.claim_interface(device, 0)
            self.device = device
        except usb.core.USBError as exc:
            if device is not None:
                usb.util.dispose_resources(device)
            if exc.errno in (13, 16) or exc.backend_error_code in (-3, -6):
                raise DeviceBusyError("Cannot claim HiDock. Disconnect it in HiNotes/Chrome or close the app using it; check USB permissions.") from exc
            raise HiDockError(f"Cannot open HiDock: {exc}") from exc

    def close(self):
        if self.device is not None:
            try:
                usb.util.dispose_resources(self.device)
            finally:
                self.device = None

    def write(self, data: bytes, timeout: int):
        if self.device is None:
            raise HiDockError("Device is not connected")
        try:
            written = self.device.write(0x01, data, timeout=timeout)
        except usb.core.USBError as exc:
            raise HiDockError(f"USB write failed: {exc}") from exc
        if written != len(data):
            raise HiDockError("Incomplete USB command write")

    def read(self, timeout: int) -> bytes:
        if self.device is None:
            raise HiDockError("Device is not connected")
        deadline = time.monotonic() + timeout / 1000
        while True:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                data = bytes(self.device.read(0x82, 65536, timeout=remaining))
            except usb.core.USBTimeoutError as exc:
                raise TransferTimeoutError("Timed out waiting for HiDock") from exc
            except usb.core.USBError as exc:
                raise HiDockError(f"USB read failed: {exc}") from exc
            if data:
                return data
            if time.monotonic() >= deadline:
                raise TransferTimeoutError("Timed out waiting for HiDock")
