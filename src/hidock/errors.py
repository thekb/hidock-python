class HiDockError(Exception):
    """Base error for device and protocol failures."""


class DeviceNotFoundError(HiDockError):
    pass


class DeviceBusyError(HiDockError):
    pass


class TransferTimeoutError(HiDockError):
    pass


class ProtocolError(HiDockError):
    pass
