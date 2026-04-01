"""Hardware serial broker: holds the OS-level lock on the physical port,
fans out reads to all connected clients, and serialises writes from clients."""

import json
import logging
import os
import queue
import socket
import struct
import sys
import tempfile
import threading

import serial

from .protocol import (
    MSG_HEADER_SIZE,
    MsgType,
    decode_header,
    encode_msg,
)

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"


def get_socket_path(port: str) -> str:
    """Return the IPC address for *port*.

    POSIX: path to a Unix-domain socket file.
    Windows: path to a small text file that contains the TCP port number.
    """
    for ch in r"/\:":
        port = port.replace(ch, "_")
    norm = port

    if _IS_WINDOWS:
        return os.path.join(tempfile.gettempdir(), f"pyserial_mux_{norm}.txt")
    return f"/tmp/pyserial_mux_{norm}.sock"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, returning b'' on clean close."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


class _ClientHandler:
    """Manages a single connected proxy client."""

    def __init__(self, conn: socket.socket, addr, broker: "BrokerServer"):
        self._conn = conn
        self._addr = addr
        self._broker = broker
        self.client_id: str | None = None
        self.target_id: str | None = None
        self.virtual_interface: str | None = None
        self.is_virtual_host: bool = False
        self.ready: bool = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"broker-client-{addr}"
        )

    def start(self):
        self._thread.start()

    def send(self, data: bytes):
        """Send raw bytes to this client (best-effort)."""
        try:
            self._conn.sendall(data)
        except OSError:
            pass

    def _run(self):
        try:
            self._handle_loop()
        except Exception as exc:
            log.debug("Client handler error: %s", exc)
        finally:
            self._broker._remove_client(self)
            try:
                self._conn.close()
            except OSError:
                pass

    def _handle_loop(self):
        while True:
            header = _recv_exact(self._conn, MSG_HEADER_SIZE)
            if not header:
                break

            msg_type, length = decode_header(header)
            payload = _recv_exact(self._conn, length) if length else b""
            if length and len(payload) < length:
                break  # connection closed mid-message

            if msg_type == MsgType.WRITE:
                self._broker._handle_write(self, payload)

            elif msg_type == MsgType.WRITE_TO:
                self._broker._handle_write_to(self, payload)

            elif msg_type == MsgType.KV_SET:
                self._broker._handle_kv_set(self, payload)

            elif msg_type == MsgType.KV_GET:
                self._broker._handle_kv_get(self, payload)

            elif msg_type == MsgType.CONFIG:
                try:
                    cfg = json.loads(payload.decode())
                    self._broker._apply_config(cfg, self)
                except Exception as exc:
                    self.send(encode_msg(MsgType.ERROR, str(exc).encode()))

            elif msg_type == MsgType.CLOSE:
                break

            else:
                log.debug("Unexpected msg type from client: %s", msg_type)


class BrokerServer:
    """Holds the serial port and serves multiple proxy clients."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        virtual_interface: str | None = None,
        debug: bool = False,
    ):
        if debug:
            logging.basicConfig(level=logging.DEBUG)
            log.setLevel(logging.DEBUG)
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._xonxoff = xonxoff
        self._rtscts = rtscts
        self._dsrdtr = dsrdtr
        self._virtual_interface = virtual_interface

        self._clients: list[_ClientHandler] = []
        self._clients_lock = threading.Lock()
        self._write_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._virtual_hosts: dict[str, _ClientHandler] = {}
        self._kv_store: dict[str, bytes] = {}
        self._kv_lock = threading.Lock()
        self._last_ids: list[str] = []

        self._serial: serial.Serial | None = None
        if not self._virtual_interface:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                xonxoff=xonxoff,
                rtscts=rtscts,
                dsrdtr=dsrdtr,
            )

        self._socket_path = get_socket_path(port)
        self._server_sock = self._create_server_socket()

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    def _apply_config(self, cfg: dict, client: _ClientHandler):
        requested_baud = int(cfg.get("baudrate", self._baudrate))
        ignore_baud_diff = bool(cfg.get("ignore_baudrate_diff", False))
        if requested_baud != self._baudrate and not ignore_baud_diff:
            raise ValueError(
                f"Baudrate mismatch: broker is using {self._baudrate} "
                f"but client requested {requested_baud}"
            )

        iface = cfg.get("virtual_interface") or self._virtual_interface
        
        if "client_id" in cfg:
            client.client_id = str(cfg["client_id"]) if cfg["client_id"] is not None else None

        if "target_id" in cfg:
            target_id = cfg["target_id"]
            if target_id is not None:
                if not client.client_id:
                    raise ValueError("target_id requires non-empty client_id")
                client.target_id = str(target_id)
            else:
                client.target_id = None
        
        if iface:
            if not client.client_id:
                raise ValueError("virtual_interface requires non-empty client_id")
            client.virtual_interface = str(iface)
            client.is_virtual_host = bool(cfg.get("host_virtual_interface", False))
            if client.is_virtual_host:
                current_host = self._virtual_hosts.get(client.virtual_interface)
                if current_host is not None and current_host is not client:
                    raise ValueError(
                        f"Virtual interface {client.virtual_interface!r} already has a host"
                    )
                self._virtual_hosts[client.virtual_interface] = client
        
        client.send(encode_msg(MsgType.ACK))
        client.ready = True
        self._broadcast_client_list(newcomer=client)

    # ------------------------------------------------------------------
    # Socket setup
    # ------------------------------------------------------------------

    def _create_server_socket(self) -> socket.socket:
        if _IS_WINDOWS:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            tcp_port = sock.getsockname()[1]
            with open(self._socket_path, "w") as f:
                f.write(str(tcp_port))
        else:
            # Remove stale socket file if present
            try:
                os.unlink(self._socket_path)
            except FileNotFoundError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(self._socket_path)
        sock.listen(64)
        return sock

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _add_client(self, handler: _ClientHandler):
        with self._clients_lock:
            self._clients.append(handler)

    def _remove_client(self, handler: _ClientHandler):
        with self._clients_lock:
            try:
                self._clients.remove(handler)
            except ValueError:
                pass
            if handler.virtual_interface:
                host = self._virtual_hosts.get(handler.virtual_interface)
                if host is handler:
                    del self._virtual_hosts[handler.virtual_interface]
            remaining = len(self._clients)
        
        self._broadcast_client_list()

        if remaining == 0:
            log.debug("Last client disconnected – shutting down broker")
            self._stop_event.set()
            # Unblock accept() by connecting to ourselves
            try:
                self._server_sock.close()
            except OSError:
                pass

    def _broadcast_client_list(self, newcomer: _ClientHandler | None = None):
        """Send MsgType.LIST_CLIENTS to everyone if list changed, or just to newcomer."""
        with self._clients_lock:
            ids = sorted(list(set(c.client_id for c in self._clients if c.client_id)))
            changed = (ids != self._last_ids)
            self._last_ids = ids
            targets = list(self._clients)
        
        msg = encode_msg(MsgType.LIST_CLIENTS, json.dumps(ids).encode())
        for client in targets:
            if not client.ready:
                continue
            if changed or client is newcomer:
                client.send(msg)

    def _broadcast(self, data: bytes, *, predicate=None):
        msg = encode_msg(MsgType.DATA, data)
        with self._clients_lock:
            targets = list(self._clients)
        for client in targets:
            if not client.ready:
                continue
            if client.target_id is not None:
                # Targeted clients are isolated from general broadcasts
                continue
            if predicate is not None and not predicate(client):
                continue
            client.send(msg)

    def _handle_write(self, sender: _ClientHandler, payload: bytes):
        if sender.target_id:
            with self._clients_lock:
                targets = [c for c in self._clients if c.client_id == sender.target_id]
            if not targets:
                log.debug("No targets found for target_id %r", sender.target_id)
                return
            msg = encode_msg(MsgType.DATA, payload)
            for target in targets:
                target.send(msg)
            return

        iface = sender.virtual_interface
        if iface:
            if sender.is_virtual_host:
                self._broadcast(
                    payload,
                    predicate=lambda c: c.virtual_interface == iface and c is not sender,
                )
                return
            host = self._virtual_hosts.get(iface)
            if host is None:
                sender.send(
                    encode_msg(
                        MsgType.ERROR,
                        f"No host connected for virtual interface {iface!r}".encode(),
                    )
                )
                return
            host.send(encode_msg(MsgType.DATA, payload))
            return
        self._write_queue.put(payload)

    def _handle_write_to(self, sender: _ClientHandler, payload: bytes):
        """Handle a targeted write from a specific client message."""
        if not payload:
            return
        tid_len = payload[0]
        if len(payload) < 1 + tid_len:
            return
        target_id_bytes = payload[1 : 1 + tid_len]
        target_id = target_id_bytes.decode()
        data = payload[1 + tid_len :]

        with self._clients_lock:
            targets = [c for c in self._clients if c.client_id == target_id]

        if not targets:
            log.debug("No targets found for targeted write to %r", target_id)
            return

        msg = encode_msg(MsgType.DATA, data)
        for target in targets:
            target.send(msg)

    def _handle_kv_set(self, sender: _ClientHandler, payload: bytes):
        """Update a key in the shared store and broadcast it."""
        if not payload:
            return
        key_len = payload[0]
        if len(payload) < 1 + key_len:
            return
        key = payload[1 : 1 + key_len].decode()
        value = payload[1 + key_len :]

        with self._kv_lock:
            self._kv_store[key] = value

        # Broadcast update to everyone (including sender, simplifies local state)
        update_msg = encode_msg(MsgType.KV_UPDATE, payload)
        with self._clients_lock:
            targets = list(self._clients)
        for client in targets:
            if client.ready:
                client.send(update_msg)

    def _handle_kv_get(self, sender: _ClientHandler, payload: bytes):
        """Retrieve one or all keys from the shared store."""
        if not payload:
            # Send ALL keys
            with self._kv_lock:
                items = list(self._kv_store.items())
            for key, value in items:
                k_bytes = key.encode()
                p = struct.pack("B", len(k_bytes)) + k_bytes + value
                sender.send(encode_msg(MsgType.KV_UPDATE, p))
            return

        key = payload.decode()
        with self._kv_lock:
            value = self._kv_store.get(key)

        if value is not None:
            k_bytes = key.encode()
            p = struct.pack("B", len(k_bytes)) + k_bytes + value
            sender.send(encode_msg(MsgType.KV_UPDATE, p))

    # ------------------------------------------------------------------
    # Background threads
    # ------------------------------------------------------------------

    def _reader_thread(self):
        """Continuously read from the serial port and broadcast to clients."""
        while not self._stop_event.is_set():
            try:
                data = self._serial.read(self._serial.in_waiting or 1)
                if data:
                    self._broadcast(data)
            except serial.SerialException as exc:
                log.error("Serial read error: %s", exc)
                self._stop_event.set()
                break

    def _writer_thread(self):
        """Dequeue write payloads and send them to the serial port in order."""
        while not self._stop_event.is_set():
            try:
                payload = self._write_queue.get(timeout=0.1)
                self._serial.write(payload)
            except queue.Empty:
                continue
            except serial.SerialException as exc:
                log.error("Serial write error: %s", exc)
                self._stop_event.set()
                break

    def _accept_thread(self):
        """Accept incoming client connections."""
        while not self._stop_event.is_set():
            try:
                conn, addr = self._server_sock.accept()
            except OSError:
                break
            handler = _ClientHandler(conn, addr, self)
            self._add_client(handler)
            handler.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start all broker threads and block until shutdown."""
        thread_specs = [(self._accept_thread, "broker-acceptor")]
        if self._serial is not None:
            thread_specs = [
                (self._reader_thread, "broker-reader"),
                (self._writer_thread, "broker-writer"),
                (self._accept_thread, "broker-acceptor"),
            ]

        for target, name in thread_specs:
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()

        self._stop_event.wait()
        self._cleanup()

    def _cleanup(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass


def run_broker(port: str, baudrate: int = 9600, **kwargs):
    """Entry point used by the subprocess launcher."""
    broker = BrokerServer(port, baudrate, **kwargs)
    broker.start()
