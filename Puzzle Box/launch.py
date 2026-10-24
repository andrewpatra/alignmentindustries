#!/usr/bin/env python3
"""Run wave_match.py continuously; toggle DSI-1 on microswitch activity."""

import subprocess
import sys
import time
from gpiozero import DigitalInputDevice

DISPLAY_OUTPUT = "DSI-1"
GPIO_PIN = 24
INACTIVITY_TIMEOUT = 15 * 60
SCRIPT_PATH = "/home/hericus/Desktop/wave_match.py"


def set_display(enabled):
    command = ["wlr-randr", "--output", DISPLAY_OUTPUT, "--on" if enabled else "--off"]
    try:
        subprocess.run(command, check=True, timeout=10)
        print(f"Display {'on' if enabled else 'off'}", flush=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"Display command failed: {exc}", file=sys.stderr, flush=True)
        return False


def main():
    # COM -> GND, NO -> GPIO24 (physical pin 18).
    with DigitalInputDevice(GPIO_PIN, pull_up=True, bounce_time=0.1) as trigger:
        previous_state = trigger.is_active
        last_press = None
        display_enabled = True

        # Start the game once; it is not stopped by the inactivity timer.
        game = subprocess.Popen([sys.executable, SCRIPT_PATH], start_new_session=True)
        print(f"Started wave_match.py (PID {game.pid})", flush=True)

        # Give the application a moment to create its window before blanking.
        time.sleep(2)
        if set_display(False):
            display_enabled = False

        try:
            while True:
                current_state = trigger.is_active
                now = time.monotonic()

                # Only a new press (not a held switch) resets the timer.
                if current_state and not previous_state:
                    last_press = now
                    if not display_enabled and set_display(True):
                        display_enabled = True
                    print("Microswitch pressed: inactivity timer reset", flush=True)

                previous_state = current_state

                if (display_enabled and last_press is not None
                        and now - last_press >= INACTIVITY_TIMEOUT):
                    if set_display(False):
                        display_enabled = False
                    last_press = None

                if game.poll() is not None:
                    print(f"wave_match.py exited (code {game.returncode})", file=sys.stderr, flush=True)
                    break

                time.sleep(0.05)
        finally:
            # Restore the screen for troubleshooting or normal desktop use.
            if not display_enabled:
                set_display(True)
            # Intentionally do not terminate wave_match.py here.


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Launcher stopped", flush=True)
