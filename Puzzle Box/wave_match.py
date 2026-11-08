#!/usr/bin/env python3
"""
Signal Tuner - Model 100 (Python / pygame edition)

The intro text shows for 8 seconds (INTRO_SECONDS), then the game begins.
Turn the two rotary encoders to line your wave up with the amber target wave.
Each wave is solved only when AMPLITUDE and FREQUENCY are both exactly right
(values step by 0.1). Hold the exact match for 2 s and the game moves on to the
next wave by itself (move off target and the timer resets).

Controls
  Rotary encoders : amplitude (GPIO 17/18) and frequency (GPIO 27/21)
  Keyboard        : Up/Down = amplitude, Left/Right = frequency (0.1 steps)
                    1 / 2 / 3 = sine / square / saw
                    Enter or Space = restart (on the final screen)
                    Esc or Q = quit
  Touch / mouse   : tap the final screen to restart

Setup (Raspberry Pi OS)
  sudo apt install python3-pygame python3-gpiozero
  (RotaryEncoder needs gpiozero 2.0+;  pip install -U gpiozero  if it is missing)

Optional: drop VT323-Regular.ttf (Google Fonts) next to this script for the
terminal look. Without it a system monospace font is used.
"""
import math
import os
import random
import sys
import time
from collections import deque

import pygame

# ============================== CONFIG ==============================
FULLSCREEN = True          # launches full screen at the display's native resolution
SCREEN_W, SCREEN_H = 800, 480   # window size, used only when FULLSCREEN = False
FPS = 30

# Encoder pins (a, b). If a knob turns the wrong way, swap its two pins.
AMP_PINS = (17, 18)
FREQ_PINS = (27, 21)

# 3-position rotary selector switch for the wave type (BCM pin numbers).
# Wire the switch's COMMON pin to GND and each position's pin to the GPIO below.
# Swap the numbers if a position selects the wrong wave. None = position unused.
# These avoid the encoder pins (17/18/27/21), the launcher microswitch (24),
# the MOSFET outputs (13/26) and I2C (2/3) used by the other scripts.
SHAPE_SWITCH_PINS = {"sine": 5, "square": 6, "saw": 12}
RESTART_BUTTON_PIN = None  # optional button (to GND) that restarts from the final screen

# Look and feel
SCROLL_SPEED = 0.03      # screen-widths per second (0 = frozen trace)
NOISE = 0.4              # px of vertical fuzz on the beam
WOBBLE = 0.005           # slow analog amplitude / baseline drift
FADE = 0.50              # phosphor decay per frame (lower = longer afterglow)
ALIGN_THRESHOLD = 0.35   # px (at 10 px per amp unit); smaller = stricter
HOLD_SECONDS = 2.0       # how long an exact match stays up before the next wave loads
INTRO_SECONDS = 8.0      # how long the SIGNAL LOST text stays up before the game begins
DIV_X, DIV_Y = 10, 8     # graticule divisions

# (shape, amplitude in 0.1 steps, frequency in 0.1 steps)  -> 35 = 3.5
LEVELS = [("sine", 35, 40), ("square", 60, 25), ("saw", 75, 60)]
SHAPES = ("sine", "square", "saw")
MAX_STEPS = 99  # 9.9

# Colors
BEIGE = (221, 208, 171)
PANEL = (231, 220, 192)
INK = (42, 33, 23)
MUTED = (110, 95, 70)
CYAN = (95, 232, 255)
CYAN_CORE = (220, 252, 255)
CYAN_TEXT = (127, 240, 255)
AMBER = (255, 178, 56)
AMBER_CORE = (255, 240, 205)
GREEN = (61, 255, 158)
GREEN_CORE = (230, 255, 240)

STATE_START, STATE_PLAY, STATE_WON = range(3)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def lerp_color(a, b, k):
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


# ============================== HARDWARE ==============================
class Hardware:
    """Rotary encoders (and optional switches) via gpiozero."""

    def __init__(self):
        self.hw_available = False
        self.amp_enc = None
        self.freq_enc = None
        self.buttons = []
        self.shape_inputs = {}  # shape name -> gpiozero Button, one per switch position
        self.events = deque()  # 'next'
        try:
            from gpiozero import RotaryEncoder

            # Amplitude: GPIO 17 & 18
            self.amp_enc = RotaryEncoder(a=AMP_PINS[0], b=AMP_PINS[1], wrap=False, max_steps=200)
            self.amp_enc.steps = 50  # Starts at 5.0
            # Frequency: GPIO 27 & 21
            self.freq_enc = RotaryEncoder(a=FREQ_PINS[0], b=FREQ_PINS[1], wrap=False, max_steps=200)
            self.freq_enc.steps = 50  # Starts at 5.0
            self.hw_available = True
        except Exception as e:
            print(f"Hardware init error: {e}")
            self.hw_available = False
            return

        try:
            from gpiozero import Button

            if RESTART_BUTTON_PIN is not None:
                b = Button(RESTART_BUTTON_PIN)
                b.when_pressed = lambda: self.events.append("next")
                self.buttons.append(b)
            # A rotary selector holds its position, so read its level instead of
            # waiting for a press event. Internal pull-ups; the active position is
            # the one connected to the common (GND) pin.
            for shape, pin in SHAPE_SWITCH_PINS.items():
                if pin is not None:
                    self.shape_inputs[shape] = Button(pin, pull_up=True, bounce_time=0.02)
        except Exception as e:
            print(f"Switch init error: {e}")

    def read_shape(self):
        """Wave type the selector switch is set to, or None between positions."""
        for shape, button in self.shape_inputs.items():
            if button.is_pressed:
                return shape
        return None

    def read(self):
        """Return (amp_steps, freq_steps) clamped to 0..99, or None."""
        if not self.hw_available:
            return None
        out = []
        for enc in (self.amp_enc, self.freq_enc):
            s = enc.steps
            c = clamp(s, 0, MAX_STEPS)
            if c != s:
                enc.steps = c  # stop the knob "winding up" past the ends
            out.append(c)
        return tuple(out)

    def set_values(self, amp_steps, freq_steps):
        if self.hw_available:
            self.amp_enc.steps = amp_steps
            self.freq_enc.steps = freq_steps


# ============================== WAVE MATH ==============================
def wave_val(shape, amp, freq, px, w, t, amp_px):
    """Wave height in pixels at column px. amp/freq are 0..9.9 values."""
    visual_freq = 1.0 + (freq / 9.9) * 3.0
    cycles = 3.0 * visual_freq
    # Traveling wave: phase = k * (x - v*t) -> same screen speed for every freq
    phase = cycles * (px / w - t * SCROLL_SPEED)
    pos = phase - math.floor(phase)
    if shape == "sine":
        v = math.sin(pos * 2 * math.pi)
    elif shape == "square":
        v = 1.0 if pos < 0.5 else -1.0
    else:
        v = pos * 2.0 - 1.0
    return v * amp * amp_px


# ============================== GAME ==============================
class Game:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("Signal Tuner - Model 100")
        if FULLSCREEN:
            # (0, 0) = use the display's current resolution
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        screen_w, screen_h = self.screen.get_size()
        if FULLSCREEN:
            pygame.mouse.set_visible(False)
        pygame.key.set_repeat(300, 40)
        self.clock = pygame.time.Clock()

        # ---- layout ----
        # The scope fills the whole display: no bezel, no panel, no on-screen controls.
        self.crt = pygame.Rect(0, 0, screen_w, screen_h)
        self.amp_px = self.crt.h / 28.0  # pixels per amp unit (9.9 stays on screen)
        cw, ch = self.crt.size

        # ---- fonts ----
        self.vt = os.path.exists(self._font_path())
        self.f_small = self.font(16)
        self.f_mid = self.font(20)
        self.f_big = self.font(30)
        self.f_bg = self.font(130)
        self.f_bg_label = self.font(20)

        # ---- pre-rendered layers ----
        self.crt_bg = self.make_crt_bg()
        self.graticule = self.make_graticule()
        self.fx = self.make_fx()
        self.hum = self.make_hum()
        self.trace = pygame.Surface(self.crt.size, pygame.SRCALPHA)
        self.scope = pygame.Surface(self.crt.size)
        self.text_cache = {}

        # ---- state ----
        self.hw = Hardware()
        self.t0 = time.time()
        self.intro_start = self.t0
        self.state = STATE_START
        self.shape = "sine"
        self.last_switch_shape = None
        self.amp_steps = 50
        self.freq_steps = 50
        self.level = 0
        self.results = []
        self.matched = False
        self.hold_start = None

    # ---------------- fonts ----------------
    @staticmethod
    def _font_path():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "VT323-Regular.ttf")

    def font(self, size):
        if self.vt:
            return pygame.font.Font(self._font_path(), size)
        # VT323 runs small; shrink sizes for the fallback font
        return pygame.font.SysFont("dejavusansmono,liberationmono,couriernew,monospace",
                                   max(8, int(size * 0.72)), bold=True)

    def blit_text(self, surf, text, font, color, pos, anchor="topleft", glow=None, alpha=255):
        img = font.render(text, True, color)
        if alpha < 255:
            img.set_alpha(alpha)
        rect = img.get_rect(**{anchor: pos})
        if glow:
            g = font.render(text, True, glow)
            g.set_alpha(70)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                surf.blit(g, rect.move(dx, dy))
        surf.blit(img, rect)
        return rect

    # ---------------- static layers ----------------
    def make_crt_bg(self):
        w, h = self.crt.size
        bg = pygame.Surface((w, h))
        outer, inner = (2, 10, 16), (11, 47, 67)
        bg.fill(outer)
        steps = 30
        for i in range(steps):
            k = i / (steps - 1)
            s = 1.15 - 0.85 * k
            r = pygame.Rect(0, 0, int(w * s), int(h * s))
            r.center = (w // 2, h // 2)
            pygame.draw.ellipse(bg, lerp_color(outer, inner, k), r)
        return bg

    def make_graticule(self):
        w, h = self.crt.size
        g = pygame.Surface((w, h), pygame.SRCALPHA)
        for c in range(DIV_X + 1):
            x = min(w - 1, round(c / DIV_X * w))
            pygame.draw.line(g, (90, 200, 240, 42), (x, 0), (x, h))
        for r in range(DIV_Y + 1):
            y = min(h - 1, round(r / DIV_Y * h))
            pygame.draw.line(g, (90, 200, 240, 42), (0, y), (w, y))
        cx, cy = w // 2, h // 2
        pygame.draw.line(g, (120, 220, 255, 90), (cx, 0), (cx, h))
        pygame.draw.line(g, (120, 220, 255, 90), (0, cy), (w, cy))
        for i in range(DIV_X * 5 + 1):  # minor ticks along the center axes
            x = min(w - 1, round(i / (DIV_X * 5) * w))
            n = 6 if i % 5 == 0 else 3
            pygame.draw.line(g, (150, 235, 255, 140), (x, cy - n), (x, cy + n))
        for i in range(DIV_Y * 5 + 1):
            y = min(h - 1, round(i / (DIV_Y * 5) * h))
            n = 6 if i % 5 == 0 else 3
            pygame.draw.line(g, (150, 235, 255, 140), (cx - n, y), (cx + n, y))
        return g

    def make_fx(self):
        """Scanlines + vignette."""
        w, h = self.crt.size
        fx = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(0, h, 3):
            pygame.draw.line(fx, (0, 0, 0, 50), (0, y), (w, y))
        for i in range(40):
            a = int(150 * (1 - i / 40) ** 2)
            pygame.draw.rect(fx, (0, 0, 0, a), pygame.Rect(i, i, w - 2 * i, h - 2 * i), 2)
        return fx

    def make_hum(self):
        s = pygame.Surface((self.crt.w, 90), pygame.SRCALPHA)
        for i in range(90):
            a = int(14 * math.sin(math.pi * i / 90))
            pygame.draw.line(s, (120, 220, 255, a), (0, i), (self.crt.w, i))
        return s

    # ---------------- input ----------------
    def adjust(self, d_amp=0, d_freq=0):
        self.amp_steps = clamp(self.amp_steps + d_amp, 0, MAX_STEPS)
        self.freq_steps = clamp(self.freq_steps + d_freq, 0, MAX_STEPS)
        self.hw.set_values(self.amp_steps, self.freq_steps)

    def press_primary(self):
        if self.state == STATE_WON:
            self.start()

    def start(self):
        self.state = STATE_PLAY
        self.level = 0
        self.results = []
        self.hold_start = None

    def next_level(self):
        # record the (exactly matched) settings for the final diagnostics
        self.results.append((self.shape, self.amp_steps / 10, self.freq_steps / 10))
        self.matched = False
        self.hold_start = None
        self.level += 1
        if self.level >= len(LEVELS):
            self.state = STATE_WON

    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    return False
                if ev.key == pygame.K_UP:
                    self.adjust(d_amp=1)
                elif ev.key == pygame.K_DOWN:
                    self.adjust(d_amp=-1)
                elif ev.key == pygame.K_RIGHT:
                    self.adjust(d_freq=1)
                elif ev.key == pygame.K_LEFT:
                    self.adjust(d_freq=-1)
                elif ev.key == pygame.K_1:
                    self.shape = "sine"
                elif ev.key == pygame.K_2:
                    self.shape = "square"
                elif ev.key == pygame.K_3:
                    self.shape = "saw"
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE):
                    self.press_primary()
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                if self.state == STATE_WON:
                    self.start()
        while self.hw.events:
            e = self.hw.events.popleft()
            if e == "next":
                self.press_primary()
        return True

    # ---------------- logic ----------------
    def update(self, now):
        hw = self.hw.read()
        if hw:
            self.amp_steps, self.freq_steps = hw

        # Selector switch: apply its position at startup and whenever it moves.
        # (Keys 1/2/3 still work; they just get replaced the next time it moves.)
        sw = self.hw.read_shape()
        if sw is not None and sw != self.last_switch_shape:
            self.shape = sw
            self.last_switch_shape = sw

        if self.state == STATE_START and now - self.intro_start >= INTRO_SECONDS:
            self.start()  # intro text has been up long enough
        if self.state != STATE_PLAY:
            self.matched = False
            return
        tgt_shape, tgt_amp, tgt_freq = LEVELS[self.level]
        self.matched = (self.shape == tgt_shape
                        and self.amp_steps == tgt_amp
                        and self.freq_steps == tgt_freq)
        if self.matched:
            if self.hold_start is None:
                self.hold_start = now
            if now - self.hold_start >= HOLD_SECONDS:
                self.next_level()  # green screen stayed up long enough: move on
        else:
            self.hold_start = None
    
    # ---------------- drawing ----------------
    def build_points(self, shape, amp, freq, t, seed):
        w, mid = self.crt.w, self.crt.h / 2
        wobble = 1 + WOBBLE * (0.6 * math.sin(t * 1.7 + seed) + 0.4 * math.sin(t * 4.3 + seed * 2))
        drift = WOBBLE * 40 * math.sin(t * 0.9 + seed)
        jump = amp * self.amp_px
        pts = []
        prev = None
        prev_y = 0.0
        for px in range(0, w + 1):
            clean = mid + drift - wave_val(shape, amp * wobble, freq, px, w, t, self.amp_px)
            y = clean + random.uniform(-0.5, 0.5) * NOISE
            if prev is not None and shape != "sine" and abs(clean - prev) > jump:
                pts.append((px, prev_y))  # square/saw jumps become vertical edges
            pts.append((px, y))
            prev, prev_y = clean, y
        return pts

    def align_runs(self, t, tgt):
        """Stretches where your wave and the target coincide."""
        w, mid = self.crt.w, self.crt.h / 2
        thr = ALIGN_THRESHOLD * self.amp_px / 10.0
        ua, uf = self.amp_steps / 10, self.freq_steps / 10
        runs, run = [], []
        for px in range(0, w + 1):
            vt = wave_val(tgt[0], tgt[1] / 10, tgt[2] / 10, px, w, t, self.amp_px)
            vu = wave_val(self.shape, ua, uf, px, w, t, self.amp_px)
            if abs(vt - vu) < thr:
                run.append((px, mid - vu))
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        return runs

    def draw_beam(self, pts, rgb, core):
        if len(pts) < 2:
            return
        pygame.draw.lines(self.trace, (*rgb, 38), False, pts, 8)
        pygame.draw.lines(self.trace, (*rgb, 95), False, pts, 4)
        pygame.draw.lines(self.trace, (*core, 255), False, pts, 2)

    def bg_number(self, text, color, alpha):
        key = (text, color, alpha)
        img = self.text_cache.get(key)
        if img is None:
            img = self.f_bg.render(text, True, color)
            img.set_alpha(alpha)
            self.text_cache[key] = img
        return img

    def draw_scope(self, t):
        sc = self.scope
        w, h = self.crt.size
        sc.blit(self.crt_bg, (0, 0))
        sc.blit(self.graticule, (0, 0))

        playing = self.state == STATE_PLAY

        # big background values (green when matched)
        if playing:
            col, alpha = (GREEN, 97) if self.matched else (CYAN, 41)
            for frac, label, steps in ((0.25, "AMP", self.amp_steps), (0.75, "FREQ", self.freq_steps)):
                cx = int(w * frac)
                num = self.bg_number(f"{steps / 10:.1f}", col, alpha)
                sc.blit(num, num.get_rect(center=(cx, h // 2 + 8)))
                lab = self.f_bg_label.render(label, True, col)
                lab.set_alpha(alpha)
                sc.blit(lab, lab.get_rect(center=(cx, h // 2 - 56)))

        # phosphor persistence: fade last frame instead of clearing it
        self.trace.fill((0, 0, 0, int(255 * FADE)), special_flags=pygame.BLEND_RGBA_SUB)
        tgt = LEVELS[self.level] if playing else None
        if playing:
            self.draw_beam(self.build_points(tgt[0], tgt[1] / 10, tgt[2] / 10, t, 1.3), AMBER, AMBER_CORE)
        self.draw_beam(self.build_points(self.shape, self.amp_steps / 10, self.freq_steps / 10, t, 0.0),
                       CYAN, CYAN_CORE)
        if playing:
            for run in self.align_runs(t, tgt):
                if len(run) >= 2:
                    self.draw_beam(run, GREEN, GREEN_CORE)
                else:
                    x, y = run[0]
                    pygame.draw.circle(self.trace, (*GREEN, 120), (x, int(y)), 4)
                    pygame.draw.circle(self.trace, (*GREEN_CORE, 255), (x, int(y)), 2)
        sc.blit(self.trace, (0, 0))

        # on-screen text
        if playing:
            self.blit_text(sc, f"{self.shape.upper()}   AMP {self.amp_steps / 10:.1f}   FREQ {self.freq_steps / 10:.1f}",
                           self.f_small, CYAN_TEXT, (12, 8), glow=CYAN)
            if not self.hw.hw_available:
                self.blit_text(sc, "KEYBOARD MODE", self.f_small, CYAN_TEXT, (w - 12, 8),
                               "topright", alpha=120)
            r = self.blit_text(sc, "CH1 TARGET", self.f_small, AMBER, (12, h - 8), "bottomleft", glow=AMBER)
            self.blit_text(sc, "CH2 SIGNAL", self.f_small, CYAN_TEXT, (r.right + 16, h - 8), "bottomleft", glow=CYAN)
        self.blit_text(sc, "2 V/DIV   0.5 ms/DIV", self.f_small, CYAN_TEXT, (w - 12, h - 8),
                       "bottomright", alpha=150)

        # glass: scanlines, vignette, slow rolling hum bar
        sc.blit(self.fx, (0, 0))
        sc.blit(self.hum, (0, (t * (h + 90) / 7.0) % (h + 90) - 90))

        # start / win overlay
        if self.state != STATE_PLAY:
            ov = pygame.Surface((w, h), pygame.SRCALPHA)
            ov.fill((2, 10, 16, 224))
            sc.blit(ov, (0, 0))
            if self.state == STATE_START:
                self.blit_text(sc, "SIGNAL LOST", self.f_big, CYAN_TEXT, (w // 2, h // 2 - 22), "center", glow=CYAN)
                self.blit_text(sc, "ALIGN THE WAVES TO REESTABLISH COMMUNICATIONS.", self.f_small,
                               CYAN_TEXT, (w // 2, h // 2 + 20), "center", glow=CYAN)
            else:
                self.blit_text(sc, "COMMUNICATIONS RESTORED", self.f_big, CYAN_TEXT,
                               (w // 2, 34), "center", glow=CYAN)
                self.blit_text(sc, "ALL THREE SIGNALS SYNCHRONIZED", self.f_small, CYAN_TEXT,
                               (w // 2, 66), "center", glow=CYAN)
                x = w // 2 - 190
                self.blit_text(sc, "WAVE DIAGNOSTICS:", self.f_mid, CYAN_TEXT, (x, 100), glow=CYAN)
                for i, (shape, a, f) in enumerate(self.results):
                    line = f"{shape.upper():<6} - AMPLITUDE: {a:.1f}  FREQUENCY: {f:.1f}"
                    self.blit_text(sc, line, self.f_mid, CYAN_TEXT, (x, 134 + i * 30), glow=CYAN)
                self.blit_text(sc, "PRESS ENTER OR TAP TO RESTART", self.f_small, CYAN_TEXT,
                               (w // 2, h - 22), "center", alpha=150)

        self.screen.blit(sc, self.crt.topleft)

    def draw(self, t):
        # The scope is the whole display: no bezel, header, knobs or panel.
        self.draw_scope(t)
        pygame.display.flip()

    def run(self):
        running = True
        while running:
            now = time.time()
            running = self.handle_events()
            self.update(now)
            self.draw(now - self.t0)
            self.clock.tick(FPS)
        pygame.quit()


if __name__ == "__main__":
    try:
        Game().run()
    except KeyboardInterrupt:
        pygame.quit()
        sys.exit(0)


