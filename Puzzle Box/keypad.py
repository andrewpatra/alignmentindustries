#!/usr/bin/env python3
"""SparkFun Qwiic Keypad (I2C) controlling two active-HIGH MOSFET inputs.

Enter a code followed by #. Press * to clear the entered digits.
Each valid code produces a one-second HIGH pulse on its output.

Wiring: keypad on I2C bus 1 (GPIO2 = SDA, GPIO3 = SCL), default address 0x4B.
MOSFET inputs on BCM GPIO 13 and 26.
"""

import time
from gpiozero import OutputDevice

try:
    from smbus2 import SMBus
except ImportError:  # fall back to python3-smbus from apt
    from smbus import SMBus

# I2C settings for the SparkFun Qwiic Keypad.
I2C_BUS = 1  # /dev/i2c-1, the Pi's built-in I2C on GPIO2/GPIO3
KEYPAD_ADDRESS = 0x4B
REG_ID = 0x00            # reads back 0x31 on this keypad
REG_BUTTON = 0x03        # oldest unread key press (ASCII), 0 if none
REG_UPDATE_FIFO = 0x06   # write 1 to advance to the next queued key
EXPECTED_ID = 0x31

# BCM GPIO numbering (not physical header pin numbers).
MOSFET_1_PIN = 13
MOSFET_2_PIN = 26

CODE_1 = "1234"  # Replace with your first code.
CODE_2 = "5678"  # Replace with your second code.
PULSE_SECONDS = 1.0
POLL_INTERVAL = 0.02
MAX_CODE_LENGTH = 12


def read_key(bus):
    """Return the next queued key as a character, or None if none is waiting."""
    bus.write_byte_data(KEYPAD_ADDRESS, REG_UPDATE_FIFO, 0x01)
    time.sleep(0.002)
    value = bus.read_byte_data(KEYPAD_ADDRESS, REG_BUTTON)
    return chr(value) if value else None


def main():
    if CODE_1 == CODE_2:
        raise ValueError("CODE_1 and CODE_2 must be different")

    bus = SMBus(I2C_BUS)
    try:
        device_id = bus.read_byte_data(KEYPAD_ADDRESS, REG_ID)
        if device_id != EXPECTED_ID:
            print(f"Warning: keypad ID is 0x{device_id:02X}, expected 0x{EXPECTED_ID:02X}",
                  flush=True)
    except OSError as err:
        bus.close()
        raise SystemExit(f"Can't reach keypad at 0x{KEYPAD_ADDRESS:02X}: {err}\n"
                         "Check wiring and that I2C is enabled (i2cdetect -y 1).")

    mosfet_1 = OutputDevice(MOSFET_1_PIN, active_high=True, initial_value=False)
    mosfet_2 = OutputDevice(MOSFET_2_PIN, active_high=True, initial_value=False)

    outputs = {CODE_1: mosfet_1, CODE_2: mosfet_2}
    pulse_end = {mosfet_1: None, mosfet_2: None}
    entered = ""

    # Discard any presses queued before the script started.
    try:
        while read_key(bus) is not None:
            pass
    except OSError:
        pass

    print("Keypad ready. Enter code then #; * clears.", flush=True)
    try:
        while True:
            now = time.monotonic()
            for output, deadline in pulse_end.items():
                if deadline is not None and now >= deadline:
                    output.off()
                    pulse_end[output] = None

            try:
                # Drain every key press queued since the last poll.
                while True:
                    key = read_key(bus)
                    if key is None:
                        break
                    if key == "*":
                        entered = ""
                        print("Input cleared", flush=True)
                    elif key == "#":
                        output = outputs.get(entered)
                        if output is not None:
                            output.on()
                            pulse_end[output] = time.monotonic() + PULSE_SECONDS
                            print("Valid code: MOSFET output HIGH for 1 second", flush=True)
                        else:
                            print("Invalid code", flush=True)
                        entered = ""
                    elif len(entered) < MAX_CODE_LENGTH:
                        entered += key
            except OSError as err:
                # Loose connector or bus glitch: report it and keep running.
                print(f"I2C error: {err}", flush=True)
                time.sleep(0.5)

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("Stopping keypad controller", flush=True)
    finally:
        mosfet_1.off()
        mosfet_2.off()
        mosfet_1.close()
        mosfet_2.close()
        bus.close()


if __name__ == "__main__":
    main()

