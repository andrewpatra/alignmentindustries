#!/usr/bin/env python3
"""Run wave_match and keypad; reset both after 15 minutes without a switch press."""

import os
import signal
import subprocess
import sys
import time
from gpiozero import DigitalInputDevice

DISPLAY_OUTPUT = "DSI-1"
GPIO_PIN = 24
INACTIVITY_TIMEOUT = 15 * 60
STARTUP_DELAY = 2
WAVE_MATCH_PATH = "/home/hericus/Desktop/alignmentindustries/Puzzle Box/wave_match.py"
KEYPAD_PATH = "/home/hericus/Desktop/alignmentindustries/Puzzle Box/keypad.py"


def set_display(enabled):
    command = ["wlr-randr", "--output", DISPLAY_OUTPUT, "--on" if enabled else "--off"]
    try:
        subprocess.run(command, check=True, timeout=10)
        print(f"Display {'on' if enabled else 'off'}", flush=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"Display command failed: {exc}", file=sys.stderr, flush=True)
        return False


def start_apps():
    processes = []
    try:
        for name, path in (("wave_match.py", WAVE_MATCH_PATH), ("keypad.py", KEYPAD_PATH)):
            process = subprocess.Popen([sys.executable, path], start_new_session=True)
            processes.append((name, process))
            print(f"Started {name} (PID {process.pid})", flush=True)
        return processes
    except Exception:
        stop_apps(processes)
        raise


def stop_apps(processes):
    # Each application has its own process group, including ordinary child processes.
    for name, process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            print(f"Stopping {name} (PID {process.pid})", flush=True)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    for name, process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # Kill any remaining members of the original process groups, even if the
    # direct Python process has already exited.
    for name, process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()


def main():
    processes = []
    display_enabled = True
    with DigitalInputDevice(GPIO_PIN, pull_up=True, bounce_time=0.1) as trigger:
        previous_state = trigger.is_active
        last_press = None
        try:
            processes = start_apps()
            time.sleep(STARTUP_DELAY)
            if set_display(False):
                display_enabled = False
            while True:
                current_state = trigger.is_active
                now = time.monotonic()

                if current_state and not previous_state:
                    last_press = now
                    if not display_enabled and set_display(True):
                        display_enabled = True
                    print("Microswitch pressed: inactivity timer reset", flush=True)
                previous_state = current_state

                if last_press is not None and now - last_press >= INACTIVITY_TIMEOUT:
                    print("15 minutes without a press: resetting both applications", flush=True)
                    if set_display(False):
                        display_enabled = False
                    stop_apps(processes)
                    processes = []
                    processes = start_apps()
                    last_press = None
                    # Ignore a switch held down during the reset until released.
                    previous_state = trigger.is_active
                    print("Reset complete; waiting for next microswitch press", flush=True)

                for name, process in processes:
                    if process.poll() is not None:
                        raise RuntimeError(f"{name} exited unexpectedly (code {process.returncode})")
                time.sleep(0.05)
        finally:
            stop_apps(processes)
            # On intentional exit or failure, restore the display for troubleshooting.
            if not display_enabled:
                set_display(True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Launcher stopped", flush=True)
    except Exception as exc:
        print(f"Launcher error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
