# pySerialMux

Drop-in 1-to-N multiplexer for `pyserial` hardware serial interfaces.

## Quick usage

```python
from pySerialMux import Serial

with Serial("/dev/ttyUSB0", baudrate=115200, timeout=1.0) as ser:
  ser.write(b"hello\n")
  print(ser.readline())
```

## On-demand QoL and debug options

The proxy accepts optional flags through `Serial(...)`:

- `debug=True`  
  Enables debug logging for proxy/broker lifecycle details.

- `logs=True`  
  Enables global binary message logging. The client will receive and buffer
  all serial and client-to-client traffic with timestamps and origin IDs.

- `ignore_baudrate_diff=True`  
  Allows a client to connect even when its requested baudrate differs from the
  broker's baudrate.

- `virtual_interface="<name>"`  
  Enables virtual routing mode for that interface name.

- `client_id="<id>"`  
  Optional, but required when using `virtual_interface` or `target_id` features.

- `target_id="<id>"`  
  Routes all writes directly to the client with the matching `client_id` instead 
  of physical serial. This client will also be isolated from general serial 
  broadcasts.

- `host_virtual_interface=True`  
  Marks the client as host for that virtual interface. Non-host clients writing
  to that interface are routed to the host client instead of physical serial.

## Client-to-client targeting

Clients can communicate directly by using `target_id`:

```python
# Setup two clients
alpha = Serial("COM1", client_id="Alpha", target_id="Beta")
beta  = Serial("COM1", client_id="Beta",  target_id="Alpha")

alpha.write(b"Hello Beta")
print(beta.read(10)) # b'Hello Beta'
```

### Runtime targeting

You can change or clear the target at any time:

```python
ser = Serial("COM1", client_id="Alpha")
ser.target_id = "Beta" # Now talking to Beta
ser.target_id = None   # Back to physical serial
```

### Targeted writes

Use the `target_id` keyword argument for one-off targeted messages:

```python
ser.write(b"One-off message", target_id="Beta")
```

## Discovery and Shared State

### Client Discovery

Access a live list of other connected clients:

```python
print(ser.other_clients) # ['Beta', 'Gamma']
```

### Shared Data Store

Share state information between all clients:

```python
# Client Alpha
ser.set_shared("status", b"active")

# Client Beta
print(ser.shared.get("status")) # b'active'
```

## Global Logging

When `logs=True` is enabled, the broker aggregates all traffic (serial and client-to-client) with high-precision timestamps and origin IDs.

### Accessing logs

Retrieve all buffered log entries:

```python
# Returns a list of dicts: 
# [{'timestamp': float, 'origin_type': OriginType, 'origin_id': str, 'data': bytes}, ...]
logs = ser.get_logs()
```

### Real-time logging

Hook into every message as it arrives:

```python
def my_logger(entry):
    print(f"[{entry['timestamp']}] {entry['origin_id']}: {entry['data']}")

ser.on_log = my_logger
```

### Runtime control

You can enable or disable logging at any time:

```python
ser.logs = True  # Start receiving logs
ser.logs = False # Stop receiving logs
```

## Virtual interface example

```python
# host side
host = Serial(
    "/dev/ttyUSB0",
    virtual_interface="lab-bus",
    client_id="host-1",
    host_virtual_interface=True,
)

# client side
client = Serial(
    "/dev/ttyUSB0",
    virtual_interface="lab-bus",
    client_id="client-1",
)

client.write(b"message to host")
print(host.read(15))
```
