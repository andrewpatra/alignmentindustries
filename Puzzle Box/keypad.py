#!/usr/bin/env python3
"""3x4 matrix keypad controlling two active-HIGH MOSFET inputs.

Enter a code followed by #. Press * to clear the entered digits.
Each valid code produces a one-second HIGH pulse on its output.
"""

import time
from gpiozero import DigitalInputDevice, OutputDevice

# BCM GPIO numbering (not physical header pin numbers).
# Keypad rows, top to bottom: 1 2 3 / 4 5 6 / 7 8 9 / * 0 #
ROW_PINS = (5, 6, 13, 19)
COLUMN_PINS = (16, 20, 21)

MOSFET_1_PIN = 23
MOSFET_2_PIN = 25
CODE_1 = "1234"  # Replace with your first code.
CODE_2 = "5678"  # Replace with your second code.
PULSE_SECONDS = 1.0
DEBOUNCE_SECONDS = 0.05
SCAN_INTERVAL = 0.01
MAX_CODE_LENGTH = 12

KEYS = (
    ("1", "2", "3"),
    ("4", "5", "6"),
    ("7", "8", "9"),
    ("*", "0", "#"),
)


def scan_key(rows, columns):
    """Return one pressed key, or None; ignore multiple simultaneous keys."""
    found = []
    for row_index, row in enumerate(rows):
        row.on()  # active_high=False: drive this row LOW
        time.sleep(0.001)
        for col_index, col in enumerate(columns):
            if col.is_active:  # pull-up column reads LOW when pressed
                found.append(KEYS[row_index][col_index])
        row.off()  # return row HIGH
    return found[0] if len(found) == 1 else None


def main():
    if CODE_1 == CODE_2:
        raise ValueError("CODE_1 and CODE_2 must be different")

    # Unselected rows HIGH; selected row LOW. Columns have internal pull-ups.
    rows = [OutputDevice(pin, active_high=False, initial_value=False)
            for pin in ROW_PINS]
    columns = [DigitalInputDevice(pin, pull_up=True)
               for pin in COLUMN_PINS]
    mosfet_1 = OutputDevice(MOSFET_1_PIN, active_high=True, initial_value=False)
    mosfet_2 = OutputDevice(MOSFET_2_PIN, active_high=True, initial_value=False)

    outputs = {CODE_1: mosfet_1, CODE_2: mosfet_2}
    pulse_end = {mosfet_1: None, mosfet_2: None}
    entered = ""
    last_sample = None
    stable_key = None
    sample_changed_at = time.monotonic()

    print("Keypad ready. Enter code then #; * clears.", flush=True)
    try:
        while True:
            now = time.monotonic()
            for output, deadline in pulse_end.items():
                if deadline is not None and now >= deadline:
                    output.off()
                    pulse_end[output] = None

            sample = scan_key(rows, columns)
            now = time.monotonic()
            if sample != last_sample:
                last_sample = sample
                sample_changed_at = now

            if sample != stable_key and now - sample_changed_at >= DEBOUNCE_SECONDS:
                stable_key = sample
                if stable_key is None:
                    pass
                elif stable_key == "*":
                    entered = ""
                    print("Input cleared", flush=True)
                elif stable_key == "#":
                    output = outputs.get(entered)
                    if output is not None:
                        output.on()
                        pulse_end[output] = now + PULSE_SECONDS
                        print("Valid code: MOSFET output HIGH for 1 second", flush=True)
                    else:
                        print("Invalid code", flush=True)
                    entered = ""
                elif len(entered) < MAX_CODE_LENGTH:
                    entered += stable_key

            time.sleep(SCAN_INTERVAL)
    except KeyboardInterrupt:
        print("Stopping keypad controller", flush=True)
    finally:
        mosfet_1.off()
        mosfet_2.off()
        for device in (*rows, *columns, mosfet_1, mosfet_2):
            device.close()


if __name__ == "__main__":
    main()
