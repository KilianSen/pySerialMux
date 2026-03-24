"""Unit tests for broker module (no real serial port required)."""

import sys
import unittest

from pySerialMux.broker import get_socket_path
from pySerialMux.protocol import (
    MSG_HEADER_SIZE,
    MsgType,
    decode_header,
    encode_msg,
)


class TestGetSocketPath(unittest.TestCase):

    def test_simple_port(self):
        path = get_socket_path("COM1")
        if sys.platform == "win32":
            self.assertIn("pyserial_mux_COM1", path)
            self.assertTrue(path.endswith(".txt"))
        else:
            self.assertEqual(path, "/tmp/pyserial_mux_COM1.sock")

    def test_unix_path_normalized(self):
        path = get_socket_path("/dev/ttyUSB0")
        if sys.platform != "win32":
            self.assertEqual(path, "/tmp/pyserial_mux__dev_ttyUSB0.sock")

    def test_windows_style_port_normalized(self):
        path = get_socket_path("COM3")
        if sys.platform != "win32":
            self.assertEqual(path, "/tmp/pyserial_mux_COM3.sock")

    def test_colon_normalized(self):
        r"""Colons (Windows COM ports like \\.\COM1) are replaced with underscores."""
        path = get_socket_path("COM1:")
        if sys.platform != "win32":
            self.assertNotIn(":", path)

    def test_backslash_normalized(self):
        path = get_socket_path(r"\\.\COM3")
        if sys.platform != "win32":
            self.assertNotIn("\\", path)
            self.assertNotIn("/", path[len("/tmp/"):])


class TestProtocolEncoding(unittest.TestCase):

    def test_encode_decode_roundtrip_data(self):
        payload = b"hello world"
        msg = encode_msg(MsgType.DATA, payload)
        self.assertEqual(len(msg), MSG_HEADER_SIZE + len(payload))
        msg_type, length = decode_header(msg[:MSG_HEADER_SIZE])
        self.assertEqual(msg_type, MsgType.DATA)
        self.assertEqual(length, len(payload))
        self.assertEqual(msg[MSG_HEADER_SIZE:], payload)

    def test_encode_decode_empty_payload(self):
        msg = encode_msg(MsgType.ACK)
        self.assertEqual(len(msg), MSG_HEADER_SIZE)
        msg_type, length = decode_header(msg)
        self.assertEqual(msg_type, MsgType.ACK)
        self.assertEqual(length, 0)

    def test_encode_decode_close(self):
        msg = encode_msg(MsgType.CLOSE)
        msg_type, length = decode_header(msg)
        self.assertEqual(msg_type, MsgType.CLOSE)
        self.assertEqual(length, 0)

    def test_decode_header_short_raises(self):
        with self.assertRaises(ValueError):
            decode_header(b"\x01\x00")

    def test_all_msg_types_roundtrip(self):
        for mt in MsgType:
            payload = f"test-{mt.name}".encode()
            msg = encode_msg(mt, payload)
            mt2, length = decode_header(msg[:MSG_HEADER_SIZE])
            self.assertEqual(mt2, mt)
            self.assertEqual(length, len(payload))

    def test_large_payload(self):
        payload = b"x" * 65536
        msg = encode_msg(MsgType.DATA, payload)
        mt, length = decode_header(msg[:MSG_HEADER_SIZE])
        self.assertEqual(mt, MsgType.DATA)
        self.assertEqual(length, 65536)
        self.assertEqual(msg[MSG_HEADER_SIZE:], payload)
