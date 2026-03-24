"""Entry point for launching the broker as a subprocess.

Usage::

    python -m pySerialMux._broker_entry <port> <baudrate> [<kwargs_json>]
"""

if __name__ == "__main__":
    import json
    import sys

    port = sys.argv[1]
    baudrate = int(sys.argv[2])
    kwargs = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    from pySerialMux.broker import run_broker

    run_broker(port, baudrate, **kwargs)
