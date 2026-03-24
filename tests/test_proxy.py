"""Unit tests for pySerialMux.proxy.Serial using mock sockets."""

import json
import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from pySerialMux.protocol import MsgType, encode_msg, MSG_HEADER_SIZE, decode_header
from pySerialMux.proxy import Serial


def _make_socket_pair():
    """Return (client_sock, server_sock) connected via socketpair."""
    a, b = socket.socketpair()
    return a, b


class _FakeBroker:
    """Minimal broker stub that talks over a socketpair."""

    def __init__(self, sock: socket.socket, accept_config: bool = True, error_msg: str = ""):
        self._sock = sock
        self._accept = accept_config
        self._error_msg = error_msg
        self._received: list = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def _run(self):
        # Read CONFIG
        header = self._recv_exact(MSG_HEADER_SIZE)
        if not header:
            return
        msg_type, length = decode_header(header)
        payload = self._recv_exact(length) if length else b""
        self._received.append((msg_type, payload))

        if self._accept:
            self._sock.sendall(encode_msg(MsgType.ACK))
        else:
            self._sock.sendall(encode_msg(MsgType.ERROR, self._error_msg.encode()))
            return

        # Keep reading messages
        while True:
            header = self._recv_exact(MSG_HEADER_SIZE)
            if not header:
                break
            msg_type, length = decode_header(header)
            payload = self._recv_exact(length) if length else b""
            self._received.append((msg_type, payload))
            if msg_type == MsgType.CLOSE:
                break

    def send_data(self, data: bytes):
        self._sock.sendall(encode_msg(MsgType.DATA, data))


def _build_proxy_with_fake_broker(accept=True, error_msg="", baudrate=9600):
    """Build a Serial proxy wired to a _FakeBroker, skipping real open()."""
    proxy_sock, broker_sock = _make_socket_pair()
    broker = _FakeBroker(broker_sock, accept_config=accept, error_msg=error_msg)

    proxy = Serial.__new__(Serial)
    proxy._port = "/dev/ttyUSB0"
    proxy._baudrate = baudrate
    proxy._bytesize = 8
    proxy._parity = "N"
    proxy._stopbits = 1
    proxy.timeout = None
    proxy._xonxoff = False
    proxy._rtscts = False
    proxy._dsrdtr = False
    proxy._extra_kwargs = {}
    proxy._sock = None
    proxy._closed = True
    proxy._buffer = bytearray()
    proxy._buffer_lock = threading.Lock()
    proxy._buffer_event = threading.Event()
    proxy._reader_thread = None

    # Inject socket and complete handshake manually
    proxy._sock = proxy_sock
    proxy._closed = False

    cfg = {"baudrate": baudrate, "bytesize": 8, "parity": "N", "stopbits": 1}
    proxy._sock.sendall(encode_msg(MsgType.CONFIG, json.dumps(cfg).encode()))

    from pySerialMux.protocol import MSG_HEADER_SIZE, decode_header
    from pySerialMux.proxy import _recv_exact
    header = _recv_exact(proxy._sock, MSG_HEADER_SIZE)
    msg_type, length = decode_header(header)
    payload = _recv_exact(proxy._sock, length) if length else b""

    if msg_type == MsgType.ERROR:
        proxy._sock.close()
        proxy._closed = True
        raise ValueError(payload.decode())

    # Start reader thread
    proxy._reader_thread = threading.Thread(
        target=proxy._reader_loop, daemon=True, name="proxy-reader-test"
    )
    proxy._reader_thread.start()

    return proxy, broker


class TestSerialAttributes(unittest.TestCase):

    def test_has_required_attributes(self):
        s = Serial.__new__(Serial)
        for attr in ("read", "write", "readline", "close", "open",
                     "reset_input_buffer", "reset_output_buffer"):
            self.assertTrue(callable(getattr(s, attr)), f"{attr} not callable")

    def test_in_waiting_property_exists(self):
        s = Serial.__new__(Serial)
        self.assertTrue(hasattr(Serial, "in_waiting"))

    def test_is_open_property_exists(self):
        self.assertTrue(hasattr(Serial, "is_open"))

    def test_context_manager_methods(self):
        self.assertTrue(hasattr(Serial, "__enter__"))
        self.assertTrue(hasattr(Serial, "__exit__"))


class TestSerialWithFakeBroker(unittest.TestCase):

    def setUp(self):
        self.proxy, self.broker = _build_proxy_with_fake_broker()

    def tearDown(self):
        try:
            self.proxy.close()
        except Exception:
            pass

    def test_in_waiting_initially_zero(self):
        self.assertEqual(self.proxy.in_waiting, 0)

    def test_is_open_true_after_open(self):
        self.assertTrue(self.proxy.is_open)

    def test_is_open_false_after_close(self):
        self.proxy.close()
        self.assertFalse(self.proxy.is_open)

    def test_reset_input_buffer_clears(self):
        with self.proxy._buffer_lock:
            self.proxy._buffer += b"hello"
        self.assertEqual(self.proxy.in_waiting, 5)
        self.proxy.reset_input_buffer()
        self.assertEqual(self.proxy.in_waiting, 0)

    def test_write_sends_write_message(self):
        self.proxy.write(b"hello")
        # Give broker stub time to receive
        time.sleep(0.1)
        write_msgs = [p for t, p in self.broker._received if t == MsgType.WRITE]
        self.assertTrue(any(p == b"hello" for p in write_msgs))

    def test_write_returns_length(self):
        n = self.proxy.write(b"test data")
        self.assertEqual(n, 9)

    def test_read_with_timeout_returns_empty_when_no_data(self):
        self.proxy.timeout = 0.05
        result = self.proxy.read(1)
        self.assertEqual(result, b"")

    def test_readline_with_timeout_returns_empty_when_no_data(self):
        self.proxy.timeout = 0.05
        result = self.proxy.readline()
        self.assertEqual(result, b"")

    def test_read_receives_data_from_broker(self):
        self.proxy.timeout = 0.5
        self.broker.send_data(b"hello")
        result = self.proxy.read(5)
        self.assertEqual(result, b"hello")

    def test_readline_receives_line_from_broker(self):
        self.proxy.timeout = 0.5
        self.broker.send_data(b"hello\nworld")
        line = self.proxy.readline()
        self.assertEqual(line, b"hello\n")

    def test_context_manager_closes(self):
        proxy, _ = _build_proxy_with_fake_broker()
        with proxy:
            self.assertTrue(proxy.is_open)
        self.assertFalse(proxy.is_open)

    def test_reset_output_buffer_is_noop(self):
        # Should not raise
        self.proxy.reset_output_buffer()

    def test_write_on_closed_raises(self):
        self.proxy.close()
        with self.assertRaises(OSError):
            self.proxy.write(b"data")

    def test_write_rejects_str(self):
        with self.assertRaises(TypeError):
            self.proxy.write("not bytes")


class TestSerialBrokerError(unittest.TestCase):

    def test_raises_value_error_on_broker_error(self):
        with self.assertRaises(ValueError):
            _build_proxy_with_fake_broker(accept=False, error_msg="Baudrate mismatch")
