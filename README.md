# pySerialMux

Drop-in 1-to-N multiplexer for `pyserial` hardware serial interfaces.

## Quick usage

```python
from pyserial_mux import Serial

with Serial("/dev/ttyUSB0", baudrate=115200, timeout=1.0) as ser:
    ser.write(b"hello\n")
    print(ser.readline())
```

## On-demand QoL and debug options

The proxy accepts optional flags through `Serial(...)`:

- `debug=True`  
  Enables debug logging for proxy/broker lifecycle details.

- `ignore_baudrate_diff=True`  
  Allows a client to connect even when its requested baudrate differs from the
  broker's baudrate.

- `virtual_interface="<name>"`  
  Enables virtual routing mode for that interface name.

- `client_id="<id>"`  
  Required when `virtual_interface` is used.

- `host_virtual_interface=True`  
  Marks the client as host for that virtual interface. Non-host clients writing
  to that interface are routed to the host client instead of physical serial.

### Virtual interface example

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

## CI/CD

This repository includes GitHub Actions workflows for:

- CI test matrix on push and pull requests (`.github/workflows/ci.yml`)
- Auto publish to PyPI on GitHub release publish (`.github/workflows/publish-pypi.yml`)

For PyPI publishing, configure trusted publishing (OIDC) for this repository in
the PyPI project settings and use the `pypi` GitHub environment.
