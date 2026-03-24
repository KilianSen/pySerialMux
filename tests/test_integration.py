"""Integration tests for pyserial-mux.

These tests exercise two proxy clients connecting to the same BrokerServer
over a real Unix Domain Socket. They require no physical serial hardware by
using a loopback via two virtual serial ports (socat) or by wiring up a
BrokerServer with a mock serial port.

Tests that need actual hardware or socat are skipped when not available.
"""

import json
import shutil
import socket
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from pyserial_mux.broker import BrokerServer, get_socket_path
from pyserial_mux.protocol import MsgType, encode_msg, MSG_HEADER_SIZE, decode_header


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _recv_msg(sock: socket.socket):
    header = _recv_exact(sock, MSG_HEADER_SIZE)
    if not header:
        return None, None
    msg_type, length = decode_header(header)
    payload = _recv_exact(sock, length) if length else b""
    return msg_type, payload


def _handshake(sock: socket.socket, baudrate: int = 9600):
    """Send CONFIG and return the broker's response (msg_type, payload)."""
    cfg = {"baudrate": baudrate, "bytesize": 8, "parity": "N", "stopbits": 1}
    sock.sendall(encode_msg(MsgType.CONFIG, json.dumps(cfg).encode()))
    return _recv_msg(sock)


def _connect_unix(path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    return s


class _MockSerial:
    """Minimal mock that replaces serial.Serial inside BrokerServer."""

    def __init__(self, read_queue=None):
        self.in_waiting = 0
        self._rq = read_queue or []
        self._written = bytearray()
        self._closed = False

    def read(self, n):
        if self._rq:
            return self._rq.pop(0)
        time.sleep(0.01)
        return b""

    def write(self, data):
        self._written += data
        return len(data)

    def close(self):
        self._closed = True


def _start_broker_with_mock_serial(port="/dev/ttyMOCK0", baudrate=9600, read_queue=None):
    """Patch serial.Serial and start BrokerServer in a daemon thread.

    Returns (broker, thread).
    """
    mock_serial = _MockSerial(read_queue=read_queue)
    with patch("pyserial_mux.broker.serial.Serial", return_value=mock_serial):
        broker = BrokerServer(port, baudrate)
    t = threading.Thread(target=broker.start, daemon=True)
    t.start()
    # Give broker time to start listening
    time.sleep(0.1)
    return broker, t, mock_serial


@unittest.skipIf(sys.platform == "win32", "Unix Domain Socket tests – POSIX only")
class TestBrokerTwoClients(unittest.TestCase):
    """Two proxy clients connect to the same BrokerServer."""

    def setUp(self):
        self.broker, self.broker_thread, self.mock_serial = (
            _start_broker_with_mock_serial()
        )
        self.sock_path = get_socket_path("/dev/ttyMOCK0")

    def tearDown(self):
        # Clean up any lingering sockets
        import os
        for path in (self.sock_path, self.sock_path + ".lock"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_two_clients_receive_ack(self):
        c1 = _connect_unix(self.sock_path)
        c2 = _connect_unix(self.sock_path)
        try:
            mt1, _ = _handshake(c1)
            mt2, _ = _handshake(c2)
            self.assertEqual(mt1, MsgType.ACK)
            self.assertEqual(mt2, MsgType.ACK)
        finally:
            c1.sendall(encode_msg(MsgType.CLOSE))
            c2.sendall(encode_msg(MsgType.CLOSE))
            c1.close()
            c2.close()

    def test_data_broadcast_to_both_clients(self):
        c1 = _connect_unix(self.sock_path)
        c2 = _connect_unix(self.sock_path)
        try:
            _handshake(c1)
            _handshake(c2)

            # Inject data into the broker by injecting into mock serial
            test_data = b"broadcast_test"
            self.broker._broadcast(test_data)

            c1.settimeout(1.0)
            c2.settimeout(1.0)
            mt1, p1 = _recv_msg(c1)
            mt2, p2 = _recv_msg(c2)
            self.assertEqual(mt1, MsgType.DATA)
            self.assertEqual(mt2, MsgType.DATA)
            self.assertEqual(p1, test_data)
            self.assertEqual(p2, test_data)
        finally:
            for s in (c1, c2):
                try:
                    s.sendall(encode_msg(MsgType.CLOSE))
                    s.close()
                except OSError:
                    pass

    def test_wrong_baudrate_returns_error(self):
        """Second client with mismatched baudrate gets an ERROR response."""
        c1 = _connect_unix(self.sock_path)
        c2 = _connect_unix(self.sock_path)
        try:
            mt1, _ = _handshake(c1, baudrate=9600)
            self.assertEqual(mt1, MsgType.ACK)

            # Try connecting a second client with a different baudrate
            mt2, payload = _handshake(c2, baudrate=115200)
            self.assertEqual(mt2, MsgType.ERROR)
            self.assertIn(b"9600", payload)
        finally:
            for s in (c1, c2):
                try:
                    s.sendall(encode_msg(MsgType.CLOSE))
                    s.close()
                except OSError:
                    pass

    def test_wrong_baudrate_allowed_with_ignore_option(self):
        c1 = _connect_unix(self.sock_path)
        c2 = _connect_unix(self.sock_path)
        try:
            mt1, _ = _handshake(c1, baudrate=9600)
            self.assertEqual(mt1, MsgType.ACK)

            cfg = {
                "baudrate": 115200,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "ignore_baudrate_diff": True,
            }
            c2.sendall(encode_msg(MsgType.CONFIG, json.dumps(cfg).encode()))
            mt2, _ = _recv_msg(c2)
            self.assertEqual(mt2, MsgType.ACK)
        finally:
            for s in (c1, c2):
                try:
                    s.sendall(encode_msg(MsgType.CLOSE))
                    s.close()
                except OSError:
                    pass

    def test_client_write_goes_to_serial(self):
        c1 = _connect_unix(self.sock_path)
        try:
            _handshake(c1)
            c1.sendall(encode_msg(MsgType.WRITE, b"hello"))
            # Give writer thread time to dequeue
            time.sleep(0.2)
            self.assertIn(b"hello", bytes(self.mock_serial._written))
        finally:
            c1.sendall(encode_msg(MsgType.CLOSE))
            c1.close()

    def test_virtual_interface_routes_client_to_host(self):
        c_host = _connect_unix(self.sock_path)
        c_client = _connect_unix(self.sock_path)
        try:
            host_cfg = {
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "virtual_interface": "vif-test",
                "client_id": "host-1",
                "host_virtual_interface": True,
            }
            c_host.sendall(encode_msg(MsgType.CONFIG, json.dumps(host_cfg).encode()))
            mt, _ = _recv_msg(c_host)
            self.assertEqual(mt, MsgType.ACK)

            client_cfg = {
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "N",
                "stopbits": 1,
                "virtual_interface": "vif-test",
                "client_id": "client-1",
            }
            c_client.sendall(encode_msg(MsgType.CONFIG, json.dumps(client_cfg).encode()))
            mt, _ = _recv_msg(c_client)
            self.assertEqual(mt, MsgType.ACK)

            c_client.sendall(encode_msg(MsgType.WRITE, b"hello-host"))
            mt, payload = _recv_msg(c_host)
            self.assertEqual(mt, MsgType.DATA)
            self.assertEqual(payload, b"hello-host")
        finally:
            for s in (c_host, c_client):
                try:
                    s.sendall(encode_msg(MsgType.CLOSE))
                    s.close()
                except OSError:
                    pass

@unittest.skipIf(
    shutil.which("socat") is None or sys.platform == "win32",
    "socat not available – skipping virtual serial port tests",
)
class TestWithSocat(unittest.TestCase):
    """Use socat to create a virtual serial port loopback."""

    def test_socat_loopback(self):
        import os
        import subprocess
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="pyserial_mux_socat_")
        pty_a = os.path.join(tmpdir, "pty_a")
        pty_b = os.path.join(tmpdir, "pty_b")

        proc = subprocess.Popen(
            ["socat", f"PTY,link={pty_a},raw,echo=0",
             f"PTY,link={pty_b},raw,echo=0"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for socat to create the PTYs
            for _ in range(20):
                if os.path.exists(pty_a) and os.path.exists(pty_b):
                    break
                time.sleep(0.1)
            else:
                self.skipTest("socat PTYs did not appear in time")

            from pyserial_mux.proxy import Serial

            with Serial(pty_a, baudrate=115200, timeout=1.0) as s:
                import serial as _serial
                with _serial.Serial(pty_b, baudrate=115200, timeout=1.0) as hw:
                    s.write(b"ping\n")
                    time.sleep(0.1)
                    data = hw.readline()
                    self.assertEqual(data, b"ping\n")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            import shutil as _shutil
            _shutil.rmtree(tmpdir, ignore_errors=True)
