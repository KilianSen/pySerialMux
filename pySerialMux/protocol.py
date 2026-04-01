"""Binary framing protocol for pyserial-mux IPC."""
import struct
from enum import IntEnum

MSG_HEADER_SIZE = 5


class MsgType(IntEnum):
    DATA   = 0x01  # broker -> client: bytes from serial
    WRITE  = 0x02  # client -> broker: bytes to write
    CLOSE  = 0x03  # client -> broker: disconnect gracefully
    CONFIG = 0x04  # client -> broker: JSON initial config
    ERROR  = 0x05  # broker -> client: error text
    ACK    = 0x06  # broker -> client: config accepted
    WRITE_TO = 0x07 # client -> broker: bytes to write to specific target
    LIST_CLIENTS = 0x08 # broker -> client: JSON list of active client IDs
    KV_SET = 0x09 # client -> broker: [1b key len][key][value]
    KV_GET = 0x0A # client -> broker: [key]
    KV_UPDATE = 0x0B # broker -> client: [1b key len][key][value]


def encode_msg(msg_type: MsgType, payload: bytes = b'') -> bytes:
    """Encode a message as [1 byte type][4 bytes big-endian length][N bytes payload]."""
    header = struct.pack('>BI', int(msg_type), len(payload))
    return header + payload


def decode_header(data: bytes) -> tuple:
    """Decode a 5-byte header, returning (MsgType, payload_length)."""
    if len(data) < MSG_HEADER_SIZE:
        raise ValueError(f"Header too short: {len(data)} bytes")
    msg_type_byte, length = struct.unpack('>BI', data[:MSG_HEADER_SIZE])
    return MsgType(msg_type_byte), length
