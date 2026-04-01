# A minimal interactive terminal

import threading
import argparse
import sys
import termios
import tty
from serial import *
from pySerialMux import patch_all

patch_all()

def reader_thread(mux: Serial):
    while True:
        try:
            if not mux.in_waiting:
                continue
            data = mux.read_until(b"\0", 1024)
            if data:
                print(data.decode('ascii', errors='replace'), end='')
        except:
            break

def getch():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="A minimal interactive terminal")
    parser.add_argument("port", help="The serial port to connect to")
    parser.add_argument(
        "-b",
        "--baudrate",
        type=int,
        default=3500000,
        help="The baud rate to use (default: 3500000)",
    )
    args = parser.parse_args()

    with Serial(args.port, args.baudrate) as mux:
        print(f"Connected to {args.port} at {args.baudrate} baud.")
        print("Type 'exit' followed by Enter to quit.")
        # Start reader thread
        reader = threading.Thread(target=reader_thread, args=(mux,), daemon=True)
        reader.start()
        print("> ", end='', flush=True)
        buffer = ""
        while True:
            try:
                ch = getch()
                if ch in ('\r', '\n'):
                    print()
                    if buffer.lower() == "exit":
                        break
                    mux.write(b'\n')
                    buffer = ""
                    print("> ", end='', flush=True)
                elif ord(ch) == 127:  # backspace
                    if buffer:
                        buffer = buffer[:-1]
                        print('\b \b', end='', flush=True)
                elif ch.isprintable() or ch == ' ':
                    buffer += ch
                    print(ch, end='', flush=True)
                    mux.write(ch.encode("ASCII"))
            except KeyboardInterrupt:
                exit(0)
                break
            except Exception as e:
                print(f"\nError: {e}")
                break