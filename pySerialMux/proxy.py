"""Proxy Serial class – drop-in replacement for serial.Serial.

Each instance connects to a BrokerServer over IPC (Unix Domain Socket on
POSIX, TCP localhost on Windows). The broker holds the physical port lock,
fans read data out to all proxies, and serialises writes.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

from .protocol import (
    MSG_HEADER_SIZE,
    MsgType,
    decode_header,
    encode_msg,
)

_IS_WINDOWS = sys.platform == "win32"

_CONNECT_RETRIES = 5
_CONNECT_SLEEP = 0.3

log = logging.getLogger(__name__)


def _normalize_port(port: str) -> str:
    for ch in r"/\:":
        port = port.replace(ch, "_")
    return port


def get_socket_path(port: str) -> str:
    """IPC address for *port* (mirrors broker's logic)."""
    norm = _normalize_port(port)
    if _IS_WINDOWS:
        return os.path.join(tempfile.gettempdir(), f"pyserial_mux_{norm}.txt")
    return f"/tmp/pyserial_mux_{norm}.sock"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _connect_to_broker(port: str) -> socket.socket:
    """Return a connected socket to the broker for *port*."""
    sock_path = get_socket_path(port)
    if _IS_WINDOWS:
        with open(sock_path) as f:
            tcp_port = int(f.read().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", tcp_port))
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
    return sock


class Serial:
    """Drop-in replacement for ``serial.Serial`` backed by a broker process."""

    def __init__(
        self,
        port=None,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        timeout=None,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        **kwargs,
    ):
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self.timeout = timeout
        self._xonxoff = xonxoff
        self._rtscts = rtscts
        self._dsrdtr = dsrdtr
        self._extra_kwargs = kwargs
        self._ignore_baudrate_diff = bool(kwargs.get("ignore_baudrate_diff", False))
        self._virtual_interface = kwargs.get("virtual_interface")
        self._client_id = kwargs.get("client_id")
        self._host_virtual_interface = bool(kwargs.get("host_virtual_interface", False))
        self._debug = bool(kwargs.get("debug", False))

        if self._debug:
            log.setLevel(logging.DEBUG)

        self._sock: socket.socket | None = None
        self._closed = True
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._buffer_event = threading.Event()
        self._reader_thread: threading.Thread | None = None

        if port is not None:
            self.open()

    # ------------------------------------------------------------------
    # open / close
    # ------------------------------------------------------------------

    def open(self):
        if not self._closed:
            return

        port = self._port
        sock_path = get_socket_path(port)
        lock_path = sock_path + ".lock"

        # Acquire a file-based lock so only one process starts the broker.
        lock_fd = self._acquire_start_lock(lock_path)
        try:
            sock = self._try_connect(port)
            if sock is None:
                self._spawn_broker(port)
                sock = self._retry_connect(port)
        finally:
            self._release_start_lock(lock_fd, lock_path)

        self._sock = sock
        self._closed = False

        # Send CONFIG and wait for ACK / ERROR
        cfg = {
            "baudrate": self._baudrate,
            "bytesize": self._bytesize,
            "parity": self._parity,
            "stopbits": self._stopbits,
            "ignore_baudrate_diff": self._ignore_baudrate_diff,
            "virtual_interface": self._virtual_interface,
            "client_id": self._client_id,
            "host_virtual_interface": self._host_virtual_interface,
        }
        if self._debug:
            log.debug("Sending broker config: %s", cfg)
        self._sock.sendall(encode_msg(MsgType.CONFIG, json.dumps(cfg).encode()))

        header = _recv_exact(self._sock, MSG_HEADER_SIZE)
        if not header:
            raise OSError("Broker closed connection during handshake")
        msg_type, length = decode_header(header)
        payload = _recv_exact(self._sock, length) if length else b""

        if msg_type == MsgType.ERROR:
            self._sock.close()
            self._closed = True
            raise ValueError(payload.decode())
        if msg_type != MsgType.ACK:
            self._sock.close()
            self._closed = True
            raise OSError(f"Unexpected handshake response: {msg_type}")

        # Start background reader
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="proxy-reader"
        )
        self._reader_thread.start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(encode_msg(MsgType.CLOSE))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        # Wake any blocking read
        self._buffer_event.set()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return not self._closed

    @property
    def in_waiting(self) -> int:
        with self._buffer_lock:
            return len(self._buffer)

    @property
    def port(self):
        return self._port

    @port.setter
    def port(self, value):
        self._port = value

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int):
        self._baudrate = value

    # ------------------------------------------------------------------
    # Read / write API
    # ------------------------------------------------------------------

    def read(self, size: int = 1) -> bytes:
        """Read up to *size* bytes, blocking until data arrives or timeout."""
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        result = bytearray()

        while len(result) < size:
            with self._buffer_lock:
                available = len(self._buffer)
                if available:
                    take = min(size - len(result), available)
                    result += self._buffer[:take]
                    del self._buffer[:take]

            if len(result) >= size:
                break

            if self._closed:
                break

            # Wait for more data
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

            self._buffer_event.clear()
            self._buffer_event.wait(timeout=remaining)

        return bytes(result)

    def readline(self, size: int = -1) -> bytes:
        """Read until newline (or *size* bytes or timeout)."""
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        result = bytearray()

        while True:
            with self._buffer_lock:
                while self._buffer:
                    byte = self._buffer[0:1]
                    del self._buffer[0]
                    result += byte
                    if byte == b"\n":
                        return bytes(result)
                    if size != -1 and len(result) >= size:
                        return bytes(result)

            if self._closed:
                break

            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

            self._buffer_event.clear()
            self._buffer_event.wait(timeout=remaining)

        return bytes(result)

    def write(self, data: bytes) -> int:
        if isinstance(data, str):
            raise TypeError("data must be bytes-like, not str")
        if self._closed:
            raise OSError("Serial is closed")
        self._sock.sendall(encode_msg(MsgType.WRITE, bytes(data)))
        return len(data)

    def reset_input_buffer(self):
        """Clear this client's local receive buffer."""
        with self._buffer_lock:
            self._buffer.clear()
        self._buffer_event.clear()

    def reset_output_buffer(self):
        """No-op: the broker's write queue cannot be cleared per-client."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reader_loop(self):
        """Background thread: receive DATA messages from broker and buffer them."""
        while not self._closed:
            try:
                header = _recv_exact(self._sock, MSG_HEADER_SIZE)
                if not header:
                    break
                msg_type, length = decode_header(header)
                payload = _recv_exact(self._sock, length) if length else b""
                if msg_type == MsgType.DATA:
                    with self._buffer_lock:
                        self._buffer += payload
                    self._buffer_event.set()
                elif msg_type == MsgType.ERROR:
                    break
            except OSError:
                break
        self._closed = True
        self._buffer_event.set()

    def _try_connect(self, port: str) -> socket.socket | None:
        try:
            return _connect_to_broker(port)
        except (OSError, FileNotFoundError, ValueError):
            return None

    def _retry_connect(self, port: str) -> socket.socket:
        for attempt in range(_CONNECT_RETRIES):
            time.sleep(_CONNECT_SLEEP)
            sock = self._try_connect(port)
            if sock is not None:
                return sock
        raise OSError(
            f"Could not connect to broker for port {port!r} "
            f"after {_CONNECT_RETRIES} retries"
        )

    def _spawn_broker(self, port: str):
        kwargs = {}
        for key in ("bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr"):
            kwargs[key] = getattr(self, f"_{key}")
        if self._virtual_interface:
            kwargs["virtual_interface"] = self._virtual_interface
        kwargs["debug"] = self._debug
        cmd = [
            sys.executable,
            "-m",
            "pySerialMux._broker_entry",
            port,
            str(self._baudrate),
            json.dumps(kwargs),
        ]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    # ------------------------------------------------------------------
    # File-lock helpers (prevent thundering-herd broker spawning)
    # ------------------------------------------------------------------

    @staticmethod
    def _acquire_start_lock(lock_path: str):
        if _IS_WINDOWS:
            import msvcrt
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            return fd
        else:
            import fcntl
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fd

    @staticmethod
    def _release_start_lock(fd, lock_path: str):
        if _IS_WINDOWS:
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
