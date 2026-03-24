"""pyserial-mux: Drop-in 1-to-N multiplexer for hardware serial interfaces."""

from .proxy import Serial
import serial as _serial


def patch_all():
    """Monkey-patch serial.Serial with the multiplexing proxy."""
    _serial.Serial = Serial


__all__ = ["Serial", "patch_all"]
