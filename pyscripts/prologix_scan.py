#!/usr/bin/python3
import argparse
import sys
import time

import serial
from serial.tools import list_ports


def find_prologix_port():
    ports = list(list_ports.comports())
    matches = []
    for port in ports:
        text = f"{port.description} {port.manufacturer} {port.product} {port.serial_number}"
        if "prologix" in text.lower():
            matches.append(port.device)
    return matches


def query_device(ser, command, wait=0.4):
    ser.reset_input_buffer()
    ser.write((command + "\n").encode("ascii"))
    time.sleep(wait)
    return ser.read_all().decode("ascii", errors="replace").strip()


def scan_addresses(ser, limit=31):
    matches = []
    for addr in range(limit):
        ser.reset_input_buffer()
        ser.write(f"++addr {addr}\n".encode("ascii"))
        time.sleep(0.15)
        ser.write(b"*IDN?\n")
        time.sleep(0.4)
        response = ser.read_all().decode("ascii", errors="replace").strip()
        if response:
            matches.append((addr, response))
    return matches


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find the Prologix serial port and the first responding GPIB address."
    )
    parser.add_argument(
        "-p",
        "--port",
        help="Prologix serial port to scan. If omitted, the script tries to auto-detect it.",
    )
    parser.add_argument(
        "-b",
        "--baudrate",
        type=int,
        default=9600,
        help="Serial baud rate for the Prologix adapter (default: 9600).",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=31,
        help="Number of GPIB addresses to scan (default: 31).",
    )
    args = parser.parse_args(argv)

    port = args.port
    if port is None:
        matches = find_prologix_port()
        if len(matches) == 1:
            port = matches[0]
        elif not matches:
            print("No Prologix adapter found in the serial port list.", file=sys.stderr)
            return 2
        else:
            print("Multiple Prologix-like ports found:", file=sys.stderr)
            for match in matches:
                print(f"  {match}", file=sys.stderr)
            print("Re-run with --port to choose one.", file=sys.stderr)
            return 2

    try:
        ser = serial.Serial(port, args.baudrate, timeout=0.5)
    except Exception as exc:
        print(f"Failed to open {port}: {exc}", file=sys.stderr)
        return 1

    try:
        version = query_device(ser, "++ver")
        print(f"port: {port}")
        if version:
            print(f"Prologix: {version}")

        matches = scan_addresses(ser, limit=args.limit)
        if not matches:
            print("No responding GPIB address found.")
            return 3

        for addr, response in matches:
            print(f"address {addr}: {response}")

        return 0
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
