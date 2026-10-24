#!/usr/bin/env python3
"""Launch an installation on microswitch presses; reset after inactivity."""

import os
import signal
import subprocess
import time

import pygame
from gpiozero import DigitalInputDevice

GPIO_PIN = 24
SCRIPT_PATH = "/home/hericus/Desktop/wave_match.py"
BLACK_IMAGE = "/home/hericus/Desktop/black.png"
INACTIVITY_TIMEOUT = 15 * 60

# COM -> GND, NO -> GPIO24. Pressed means active.
trigger = DigitalInputDevice(
    GPIO_PIN,
    pull_up=True,
    active_state=False,
    bounce_time=0.1,
)

pygame.init()
screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
pygame.mouse.set_visible(False)
black_image = pygame.image.load(BLACK_IMAGE).convert()
black_image = pygame.transform.scale(black_image, screen.get_size())

process = None
last_trigger_time = None
previous_gpio_state = trigger.is_active


def show_black():
    global screen
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.NOFRAME)
    screen.blit(black_image, (0, 0))
    pygame.display.flip()


def start_script():
    global process
    print("Microswitch pressed: starting installation", flush=True)
    pygame.display.iconify()
    process = subprocess.Popen(["python3", SCRIPT_PATH], start_new_session=True)


def stop_script():
    global process, last_trigger_time
    print("15 minutes without a press: resetting", flush=True)
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        process = None
    last_trigger_time = None
    show_black()


show_black()
try:
    while True:
        current_gpio_state = trigger.is_active
        now = time.monotonic()

        if current_gpio_state and not previous_gpio_state:
            last_trigger_time = now
            if process is None:
                start_script()
            else:
                print("Microswitch pressed: inactivity timer reset", flush=True)

        previous_gpio_state = current_gpio_state

        if last_trigger_time is not None and now - last_trigger_time >= INACTIVITY_TIMEOUT:
            stop_script()

        pygame.event.pump()
        time.sleep(0.05)
except KeyboardInterrupt:
    print("Controller stopped", flush=True)
finally:
    if process is not None:
        stop_script()
    trigger.close()
    pygame.quit()
