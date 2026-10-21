#!/usr/bin/env python3
"""
Signal Tuner - Model 100  (pygame edition)

A wave-matching puzzle for an escape room. Three levels (sine, square and saw in a
random order): dial in the target's wave type, amplitude and frequency, then hold
the match for 1.5 seconds to clear the level.

Runs on its own - no browser and no encoder_server.py needed. On a Raspberry Pi the
amplitude knob is read straight from the rotary encoder (GPIO 17 / 27 by default,
same wiring as encoder_server.py). Everywhere else it falls back to the keyboard.

    pip install pygame            # on the Pi: sudo apt install python3-pygame python3-gpiozero
    python3 wave_match.py                   # windowed, keyboard + mouse
    python3 wave_match.py --fullscreen      # for the escape-room screen
    python3 wave_match.py --invert          # if the knob turns the wrong way

Controls
    Up / Down      amplitude        (also the rotary encoder, and the mouse wheel over the dial)
    Left / Right   frequency        (mouse wheel over the dial)
    1 / 2 / 3      sine / square / saw   (or click a lamp)
    Shift          move the dials 4x faster
    R              reset            F  fullscreen      Esc / Q  quit
"""

from __future__ import annotations

import argparse
import math
import random
import threading
import time

import pygame
from pygame import gfxdraw

# --------------------------------------------------------------------------- #
#  Game constants (same values as the browser version)
# --------------------------------------------------------------------------- #
SHAPES = ("sine", "square", "saw")
SHAPE_LABELS = {"sine": "SINE", "square": "SQUARE", "saw": "SAW"}
LEVELS = 3
HOLD_SECONDS = 1.5
LOCK_THRESHOLD = 94          # percent
AMP_MIN, AMP_MAX = 0.10, 2.00
FREQ_MIN, FREQ_MAX = 0.20, 4.00

# --------------------------------------------------------------------------- #
#  Look: 1950s lab-equipment palette
# --------------------------------------------------------------------------- #
W, H = 1280, 720

CHASSIS_HI = (236, 224, 191)
CHASSIS = (221, 208, 171)
CHASSIS_LO = (185, 165, 117)
PANEL_FACE = (231, 220, 192)
PANEL_EDGE = (141, 122, 81)
INK = (42, 33, 23)
INK_DIM = (110, 95, 70)
BRASS = (169, 128, 43)
BRASS_HI = (232, 205, 132)
BRASS_DARK = (107, 79, 20)
CRT_BG = (4, 16, 11)
CRT_GRID = (18, 48, 40)
TARGET = (255, 178, 56)
USER_GLOW = (61, 255, 158)
USER_DARK = (15, 74, 44)
DANGER = (156, 42, 30)
LAMP_OFF = (132, 119, 87)
WIN_INK = (138, 106, 20)
NIXIE_ORANGE = (255, 138, 61)
GAUGE_ARC = (183, 160, 96)

UI_FONTS = "oswald,bebasneue,dejavusanscondensed,liberationsansnarrow,arialnarrow,arial,dejavusans"
MONO_FONTS = "couriernew,dejavusansmono,liberationmono,monospace"


# --------------------------------------------------------------------------- #
#  Puzzle logic (no pygame in here, so it is easy to test)
# --------------------------------------------------------------------------- #
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def ease_in_out(t):
    return 2 * t * t if t < 0.5 else -1 + (4 - 2 * t) * t


def wave_at(shape, amp, x):
    """One sample of a wave. x is measured in cycles."""
    pos = x - math.floor(x)
    if shape == "sine":
        v = math.sin(pos * 2 * math.pi)
    elif shape == "square":
        v = 1.0 if pos < 0.5 else -1.0
    else:  # saw
        v = pos * 2 - 1
    return v * amp


class Wave:
    """A wave with a shape, amplitude and frequency. Changing shape cross-fades."""

    def __init__(self, shape="sine", amp=1.0, freq=1.0):
        self.shape, self.amp, self.freq = shape, amp, freq
        self.morph = None  # (from_shape, to_shape, start_time, duration)

    def set_shape(self, shape, now, duration):
        if shape == self.shape and not self.morph:
            return
        from_shape = self.morph[1] if self.morph else self.shape
        self.morph = (from_shape, shape, now, duration)
        self.shape = shape

    def value(self, xdom, now):
        x = xdom * self.freq
        if self.morph:
            src, dst, start, duration = self.morph
            t = (now - start) / duration
            if t >= 1:
                self.morph = None
            else:
                e = ease_in_out(max(0.0, t))
                a = wave_at(src, self.amp, x)
                b = wave_at(dst, self.amp, x)
                return a + (b - a) * e
        return wave_at(self.shape, self.amp, x)


class Puzzle:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.target = Wave()
        self.user = Wave()
        self.order = list(SHAPES)
        self.level = 0
        self.done = False
        self.hold_start = None
        self.hold_elapsed = 0.0
        self.hold_frac = 0.0
        self.lock = 0
        self.status = ""
        self.status_win = False
        self.new_game(time.monotonic())

    # -- setup ------------------------------------------------------------- #
    def new_game(self, now):
        self.done = False
        self.level = 0
        self.order = list(SHAPES)
        self.rng.shuffle(self.order)
        self._load_level(now)

    def _load_level(self, now):
        self.target.set_shape(self.order[self.level], now, 0.7)
        self.target.amp = round(0.4 + self.rng.random() * 1.4, 2)
        self.target.freq = round(0.4 + self.rng.random() * 2.6, 2)
        self.hold_start = None
        self.hold_elapsed = 0.0
        self.hold_frac = 0.0
        self.status = "Match the target to start the hold."
        self.status_win = False

    # -- player input ------------------------------------------------------ #
    def set_user_shape(self, shape, now):
        self.user.set_shape(shape, now, 0.45)

    def nudge_amp(self, delta):
        self.user.amp = clamp(self.user.amp + delta, AMP_MIN, AMP_MAX)

    def nudge_freq(self, delta):
        self.user.freq = clamp(self.user.freq + delta, FREQ_MIN, FREQ_MAX)

    # -- scoring ----------------------------------------------------------- #
    def compute_match(self):
        """Dial distance, not a simulated signal comparison: how close are the controls?"""
        shape_ok = 1.0 if self.user.shape == self.target.shape else 0.0
        amp_score = max(0.0, 1 - abs(self.user.amp - self.target.amp) / 1.0)
        freq_score = max(0.0, 1 - abs(self.user.freq - self.target.freq) / 2.0)
        return round(clamp(shape_ok * (amp_score + freq_score) / 2, 0, 1) * 100)

    # -- per-frame --------------------------------------------------------- #
    def update(self, now):
        self.lock = self.compute_match()
        if self.done:
            return
        if self.lock >= LOCK_THRESHOLD:
            if self.hold_start is None:
                self.hold_start = now
            self.hold_elapsed = now - self.hold_start
            self.hold_frac = min(1.0, self.hold_elapsed / HOLD_SECONDS)
            self.status = "Locked — hold steady..."
            self.status_win = True
            if self.hold_elapsed >= HOLD_SECONDS:
                self.level += 1
                if self.level >= LEVELS:
                    self._finish()
                else:
                    self._load_level(now)
        else:
            self.hold_start = None
            self.hold_elapsed = 0.0
            self.hold_frac = 0.0
            self.status_win = False
            self.status = ("Almost — fine-tune it to lock in."
                           if self.lock >= 80 else "Match the target to start the hold.")

    def _finish(self):
        self.done = True
        self.hold_start = None
        self.hold_frac = 1.0
        self.status = "Solved — all %d waves matched. Press Reset to replay." % LEVELS
        self.status_win = True

    @property
    def badge(self):
        return "ALL CLEAR" if self.done else "LEVEL %d/%d" % (self.level + 1, LEVELS)


# --------------------------------------------------------------------------- #
#  Rotary encoder (Raspberry Pi). gpiozero calls back from its own thread, so the
#  clicks are collected under a lock and drained once per frame.
# --------------------------------------------------------------------------- #
class EncoderInput:
    def __init__(self, pin_a, pin_b, invert):
        from gpiozero import RotaryEncoder  # imported here so the game runs without it

        self._lock = threading.Lock()
        self._steps = 0
        direction = -1 if invert else 1
        self._encoder = RotaryEncoder(pin_a, pin_b, max_steps=0)
        self._encoder.when_rotated_clockwise = lambda: self._add(direction)
        self._encoder.when_rotated_counter_clockwise = lambda: self._add(-direction)

    def _add(self, n):
        with self._lock:
            self._steps += n

    def take(self):
        with self._lock:
            steps, self._steps = self._steps, 0
        return steps

    def close(self):
        self._encoder.close()


# --------------------------------------------------------------------------- #
#  Drawing helpers
# --------------------------------------------------------------------------- #
_fonts = {}


def font(size, mono=False, bold=True):
    key = (size, mono, bold)
    if key not in _fonts:
        _fonts[key] = pygame.font.SysFont(MONO_FONTS if mono else UI_FONTS, size, bold=bold)
    return _fonts[key]


def draw_text(surf, s, f, color, pos, anchor="topleft"):
    img = f.render(s, True, color)
    rect = img.get_rect(**{anchor: pos})
    surf.blit(img, rect)
    return rect


def lerp_color(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vertical_gradient(surf, rect, stops):
    for i in range(rect.height):
        t = i / max(1, rect.height - 1)
        col = stops[-1][1]
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if t <= p1:
                col = lerp_color(c0, c1, (t - p0) / (p1 - p0) if p1 > p0 else 0)
                break
        pygame.draw.line(surf, col, (rect.x, rect.y + i), (rect.right - 1, rect.y + i))


def draw_card(surf, rect):
    pygame.draw.rect(surf, PANEL_FACE, rect, border_radius=8)
    pygame.draw.rect(surf, PANEL_EDGE, rect, width=1, border_radius=8)


def draw_screw(surf, cx, cy):
    for r in range(7, 0, -1):
        gfxdraw.filled_circle(surf, cx, cy, r, lerp_color((255, 244, 214), BRASS, 1 - r / 7))
    gfxdraw.aacircle(surf, cx, cy, 7, (107, 79, 20))
    pygame.draw.line(surf, (60, 42, 12), (cx - 4, cy + 2), (cx + 4, cy - 2), 2)


# --- nixie-tube numerals ---------------------------------------------------- #
NIXIE_W, NIXIE_H, NIXIE_GAP = 24, 34, 3
_nixie_cache = {}


def nixie_cell(ch):
    if ch in _nixie_cache:
        return _nixie_cache[ch]
    pad = 5
    s = pygame.Surface((NIXIE_W + pad * 2, NIXIE_H + pad * 2), pygame.SRCALPHA)
    r = pygame.Rect(pad, pad, NIXIE_W, NIXIE_H)
    radii = dict(border_top_left_radius=12, border_top_right_radius=12,
                 border_bottom_left_radius=4, border_bottom_right_radius=4)
    pygame.draw.rect(s, (12, 8, 4), r, **radii)
    pygame.draw.ellipse(s, (36, 26, 16), pygame.Rect(pad + 4, pad + 3, NIXIE_W - 8, NIXIE_H // 2))
    pygame.draw.rect(s, (58, 42, 20), r, width=1, **radii)
    f = font(28, mono=True)
    glow = f.render(ch, True, (255, 106, 26))
    glow.set_alpha(60)
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        s.blit(glow, glow.get_rect(center=(pad + NIXIE_W // 2 + dx, pad + NIXIE_H // 2 + dy)))
    s.blit(f.render(ch, True, NIXIE_ORANGE),
           f.render(ch, True, NIXIE_ORANGE).get_rect(center=(pad + NIXIE_W // 2, pad + NIXIE_H // 2)))
    _nixie_cache[ch] = s
    return s


def draw_nixie(surf, s, cx, top):
    """Draw a row of nixie digits horizontally centred on cx."""
    total = len(s) * NIXIE_W + (len(s) - 1) * NIXIE_GAP
    x = cx - total // 2
    for ch in s:
        surf.blit(nixie_cell(ch), (x - 5, top - 5))
        x += NIXIE_W + NIXIE_GAP


# --- panel lamps ------------------------------------------------------------ #
def draw_lamp(surf, cx, cy, lit, color):
    r = 13
    if lit:
        glow = pygame.Surface((r * 6, r * 6), pygame.SRCALPHA)
        for i in range(7, 0, -1):
            gfxdraw.filled_circle(glow, r * 3, r * 3, r + i * 2, (*color, 16))
        surf.blit(glow, glow.get_rect(center=(cx, cy)))
        ring = BRASS_DARK if color == TARGET else USER_DARK
        gfxdraw.filled_circle(surf, cx, cy, r, ring)
        deep = lerp_color(color, (0, 0, 0), 0.3)
        for i in range(r - 2, 0, -1):
            gfxdraw.filled_circle(surf, cx - (r - 2 - i) // 4, cy - (r - 2 - i) // 4, i,
                                  lerp_color((255, 250, 225), deep, i / (r - 2)) if i > 3
                                  else (255, 250, 225))
    else:
        gfxdraw.filled_circle(surf, cx, cy, r, (92, 82, 54))
        for i in range(r - 2, 0, -1):
            gfxdraw.filled_circle(surf, cx - 1, cy - 1, i, lerp_color((90, 83, 64), LAMP_OFF, 1 - i / (r - 2)))
    gfxdraw.aacircle(surf, cx, cy, r, (92, 82, 54) if not lit else BRASS_DARK)


# --- radial gauge ----------------------------------------------------------- #
class Gauge:
    """Half-moon VU gauge. The face is drawn once (supersampled); the needle every frame."""

    MIN_A, MAX_A = -55.0, 55.0
    SS = 3

    def __init__(self, topleft, scale, vmin, vmax, labels, red_from=None):
        self.scale, self.vmin, self.vmax = scale, vmin, vmax
        self.topleft = topleft
        self.cx = topleft[0] + 100 * scale
        self.cy = topleft[1] + 112 * scale
        self.face = self._render_face(labels, red_from)

    def _render_face(self, labels, red_from):
        ss, u = self.SS, self.scale * self.SS
        w, h = int(200 * self.scale), int(125 * self.scale)
        big = pygame.Surface((w * ss, h * ss), pygame.SRCALPHA)

        def pt(r, ang):
            a = math.radians(ang)
            return (100 * u + r * u * math.sin(a), 112 * u - r * u * math.cos(a))

        def band(a0, a1, color):
            steps = max(2, int(abs(a1 - a0) / 2))
            angles = [a0 + (a1 - a0) * i / steps for i in range(steps + 1)]
            outer = [pt(101.5, a) for a in angles]
            inner = [pt(88.5, a) for a in reversed(angles)]
            pygame.draw.polygon(big, color, outer + inner)
            for a in (a0, a1):
                x, y = pt(95, a)
                pygame.draw.circle(big, color, (x, y), 6.5 * u)

        band(self.MIN_A, self.MAX_A, GAUGE_ARC)
        if red_from is not None:
            band(self.MIN_A + (self.MAX_A - self.MIN_A) * red_from, self.MAX_A, DANGER)

        for i in range(11):
            ang = self.MIN_A + (self.MAX_A - self.MIN_A) * i / 10
            major = i % 2 == 0
            pygame.draw.line(big, INK, pt(70 if major else 78, ang), pt(87, ang),
                             int((2 if major else 1) * u))
        lf = font(int(12 * u))
        for i, label in enumerate(labels):
            ang = self.MIN_A + (self.MAX_A - self.MIN_A) * i / (len(labels) - 1)
            draw_text(big, label, lf, INK, pt(54, ang), "center")

        return pygame.transform.smoothscale(big, (w, h))

    def paint_face(self, surf):
        surf.blit(self.face, self.topleft)

    def draw_needle(self, surf, value):
        t = clamp((value - self.vmin) / (self.vmax - self.vmin), 0, 1)
        ang = math.radians(self.MIN_A + (self.MAX_A - self.MIN_A) * t)
        s = self.scale
        dx, dy = math.sin(ang), -math.cos(ang)
        px, py = -dy, dx
        half = 1.7 * s
        tip = (self.cx + dx * 78 * s, self.cy + dy * 78 * s)
        tail = (self.cx - dx * 12 * s, self.cy - dy * 12 * s)
        poly = [tip,
                (self.cx + px * half, self.cy + py * half),
                (tail[0] + px * half, tail[1] + py * half),
                (tail[0] - px * half, tail[1] - py * half),
                (self.cx - px * half, self.cy - py * half)]
        poly = [(int(x), int(y)) for x, y in poly]
        gfxdraw.filled_polygon(surf, poly, INK)
        gfxdraw.aapolygon(surf, poly, INK)
        cx, cy = int(self.cx), int(self.cy)
        gfxdraw.filled_circle(surf, cx, cy, int(7 * s), INK)
        gfxdraw.aacircle(surf, cx, cy, int(7 * s), INK)
        gfxdraw.filled_circle(surf, cx, cy, int(3 * s), BRASS)


# --------------------------------------------------------------------------- #
#  The screen
# --------------------------------------------------------------------------- #
class Ui:
    SCOPE = pygame.Rect(68, 136, 614, 278)
    BEZEL = pygame.Rect(50, 108, 650, 318)
    LOCK_CARD = pygame.Rect(50, 442, 380, 196)
    HOLD_CARD = pygame.Rect(446, 442, 254, 196)
    TARGET_PANEL = pygame.Rect(730, 108, 500, 200)
    TRACE_PANEL = pygame.Rect(730, 324, 500, 374)
    RESET_BTN = pygame.Rect(580, 656, 120, 42)

    def __init__(self, puzzle, hint):
        self.p = puzzle
        self.hint = hint
        self.t0 = time.monotonic()
        self.lock_disp = 0.0

        self.amp_gauge = Gauge((750, 376), 1.05, AMP_MIN, AMP_MAX, ("0.1", "1.0", "2.0"))
        self.freq_gauge = Gauge((1000, 376), 1.05, FREQ_MIN, FREQ_MAX, ("0.2", "2.0", "4.0"))
        self.lock_gauge = Gauge((66, 462), 1.2, 0, 100, ("0", "50", "100"), red_from=0.85)
        self.amp_zone = pygame.Rect(750, 376, 210, 200)
        self.freq_zone = pygame.Rect(1000, 376, 210, 200)

        self.target_lamp_x = (810, 980, 1150)
        self.user_lamp_x = (810, 980, 1150)
        self.user_lamp_hits = {
            shape: pygame.Rect(x - 45, 598, 90, 70) for shape, x in zip(SHAPES, self.user_lamp_x)
        }

        self.bg = self._build_background()
        self.scope_base = self._build_scope_base()
        self.scope_surf = pygame.Surface(self.SCOPE.size)
        self.glow_surf = pygame.Surface(self.SCOPE.size, pygame.SRCALPHA)

    # -- static layer ------------------------------------------------------ #
    def _build_background(self):
        bg = pygame.Surface((W, H))
        vertical_gradient(bg, bg.get_rect(), [(0, CHASSIS_HI), (0.4, CHASSIS), (1, CHASSIS_LO)])
        pygame.draw.rect(bg, PANEL_EDGE, pygame.Rect(6, 6, W - 12, H - 12), width=2, border_radius=14)
        for cx, cy in ((24, 24), (W - 24, 24), (24, H - 24), (W - 24, H - 24)):
            draw_screw(bg, cx, cy)

        draw_text(bg, "SIGNAL TUNER", font(40), INK, (50, 22))
        draw_text(bg, "MODEL 100  ·  MATCH THE TARGET TRACE", font(16, bold=False), INK_DIM, (52, 68))
        pygame.draw.line(bg, PANEL_EDGE, (50, 94), (W - 50, 94), 2)

        # scope bezel
        pygame.draw.rect(bg, (58, 47, 30), self.BEZEL.inflate(6, 6), border_radius=11)
        pygame.draw.rect(bg, (10, 10, 8), self.BEZEL, border_radius=8)
        pygame.draw.rect(bg, (21, 17, 11), self.BEZEL, width=6, border_radius=8)
        draw_text(bg, "OSCILLOSCOPE DISPLAY", font(13, bold=False), (168, 154, 120),
                  (self.BEZEL.centerx, self.BEZEL.y + 17), "center")

        # lock + hold cards
        draw_card(bg, self.LOCK_CARD)
        draw_card(bg, self.HOLD_CARD)
        self.lock_gauge.paint_face(bg)
        draw_text(bg, "LOCK %", font(15), INK_DIM, (345, 556), "midtop")

        # target panel
        self._panel_frame(bg, self.TARGET_PANEL, "TARGET", TARGET)
        for x, shape in zip(self.target_lamp_x, SHAPES):
            pass  # lamps are dynamic
        draw_text(bg, "AMPLITUDE", font(14, bold=False), INK_DIM, (855, 282), "midtop")
        draw_text(bg, "FREQUENCY (HZ)", font(14, bold=False), INK_DIM, (1105, 282), "midtop")

        # your-trace panel
        self._panel_frame(bg, self.TRACE_PANEL, "YOUR TRACE", USER_GLOW)
        self.amp_gauge.paint_face(bg)
        self.freq_gauge.paint_face(bg)
        draw_text(bg, "AMPLITUDE", font(14, bold=False), INK_DIM, (855, 556), "midtop")
        draw_text(bg, "FREQUENCY (HZ)", font(14, bold=False), INK_DIM, (1105, 556), "midtop")
        return bg

    def _panel_frame(self, bg, rect, title, dot_color):
        draw_card(bg, rect)
        gfxdraw.filled_circle(bg, rect.x + 24, rect.y + 24, 6, dot_color)
        gfxdraw.aacircle(bg, rect.x + 24, rect.y + 24, 6, lerp_color(dot_color, INK, 0.4))
        draw_text(bg, title, font(20), INK, (rect.x + 40, rect.y + 13))
        pygame.draw.line(bg, PANEL_EDGE, (rect.x + 14, rect.y + 42), (rect.right - 14, rect.y + 42), 1)

    def _build_scope_base(self):
        cw, ch = self.SCOPE.size
        base = pygame.Surface((cw, ch))
        base.fill(CRT_BG)
        for c in range(13):
            x = int(c / 12 * (cw - 1))
            pygame.draw.line(base, CRT_GRID, (x, 0), (x, ch))
        for r in range(7):
            y = int(r / 6 * (ch - 1))
            pygame.draw.line(base, CRT_GRID, (0, y), (cw, y))
        pygame.draw.line(base, (14, 60, 42), (0, ch // 2), (cw, ch // 2))
        return base

    # -- dynamic layer ----------------------------------------------------- #
    def _draw_scope(self, surf, now):
        cw, ch = self.SCOPE.size
        s = self.scope_surf
        s.blit(self.scope_base, (0, 0))
        mid, amp_px = ch / 2, (ch / 2) * 0.85
        t = now - self.t0

        traces = []
        for wave, color in ((self.p.target, TARGET), (self.p.user, USER_GLOW)):
            pts = []
            for px in range(0, cw + 1, 2):
                xdom = (px / cw) * 3 - t * 0.15
                pts.append((px, mid - wave.value(xdom, now) * amp_px / 2))
            traces.append((pts, color))

        self.glow_surf.fill((0, 0, 0, 0))
        for pts, color in traces:
            pygame.draw.lines(self.glow_surf, (*color, 55), False, pts, 8)
        s.blit(self.glow_surf, (0, 0))
        for pts, color in traces:
            pygame.draw.lines(s, color, False, pts, 2)
        surf.blit(s, self.SCOPE.topleft)

    def _draw_badge(self, surf):
        f = font(20)
        label = self.p.badge
        w = f.size(label)[0] + 28
        rect = pygame.Rect(W - 50 - w, 32, w, 36)
        vertical_gradient(surf, rect, [(0, BRASS_HI), (1, BRASS)])
        pygame.draw.rect(surf, BRASS_DARK, rect, width=1, border_radius=3)
        draw_text(surf, label, f, (45, 34, 6), rect.center, "center")

    def _draw_lamps(self, surf, xs, cy, selected, color, label_y, labels_dim=False):
        for x, shape in zip(xs, SHAPES):
            lit = shape == selected
            draw_lamp(surf, x, cy, lit, color)
            draw_text(surf, SHAPE_LABELS[shape], font(15), INK if lit else INK_DIM, (x, label_y), "midtop")

    def render(self, surf, now, dt):
        p = self.p
        surf.blit(self.bg, (0, 0))
        self._draw_badge(surf)
        self._draw_scope(surf, now)

        # lock gauge + number
        self.lock_disp += (p.lock - self.lock_disp) * min(1.0, dt * 10)
        self.lock_gauge.draw_needle(surf, self.lock_disp)
        draw_nixie(surf, "%03d" % p.lock, 345, 506)

        # hold ladder
        lit = round(p.hold_frac * 10)
        for i in range(10):
            seg = pygame.Rect(470 + i * 21, 476, 16, 90)
            if i < lit:
                glow = pygame.Surface((seg.w + 16, seg.h + 16), pygame.SRCALPHA)
                pygame.draw.rect(glow, (*TARGET, 50), glow.get_rect(), border_radius=8)
                surf.blit(glow, (seg.x - 8, seg.y - 8))
                pygame.draw.rect(surf, (199, 123, 16), seg, border_radius=3)
                pygame.draw.rect(surf, TARGET, seg.inflate(-2, -2), border_radius=3)
            else:
                pygame.draw.rect(surf, (42, 36, 22), seg, border_radius=3)
                pygame.draw.rect(surf, (74, 63, 40), seg, width=1, border_radius=3)
        draw_text(surf, "HOLD %.1fs / %.1fs" % (p.hold_elapsed if not p.done else HOLD_SECONDS, HOLD_SECONDS),
                  font(15), INK_DIM, (self.HOLD_CARD.centerx, 590), "midtop")

        # status line + reset
        size = 20  # shrink to fit so a wider system font never runs under the Reset button
        while size > 12 and font(size, bold=p.status_win).size(p.status)[0] > self.RESET_BTN.x - 60:
            size -= 1
        draw_text(surf, p.status, font(size, bold=p.status_win), WIN_INK if p.status_win else INK_DIM,
                  (50, 677), "midleft")
        hover = self.RESET_BTN.collidepoint(pygame.mouse.get_pos())
        vertical_gradient(surf, self.RESET_BTN,
                          [(0, (90, 78, 58) if hover else (74, 64, 48)), (1, (51, 44, 28) if hover else (42, 36, 22))])
        pygame.draw.rect(surf, (26, 22, 13), self.RESET_BTN, width=1, border_radius=5)
        draw_text(surf, "RESET", font(18), (232, 220, 192), self.RESET_BTN.center, "center")

        # target panel
        self._draw_lamps(surf, self.target_lamp_x, 186, p.target.shape, TARGET, 206)
        draw_nixie(surf, "%.2f" % p.target.amp, 855, 240)
        draw_nixie(surf, "%.2f" % p.target.freq, 1105, 240)

        # player panel
        self.amp_gauge.draw_needle(surf, p.user.amp)
        self.freq_gauge.draw_needle(surf, p.user.freq)
        draw_nixie(surf, "%.2f" % p.user.amp, 855, 512)
        draw_nixie(surf, "%.2f" % p.user.freq, 1105, 512)
        self._draw_lamps(surf, self.user_lamp_x, 618, p.user.shape, USER_GLOW, 638)
        draw_text(surf, self.hint, font(14, bold=False), INK_DIM, (self.TRACE_PANEL.centerx, 676), "midtop")


# --------------------------------------------------------------------------- #
#  App: window, input, main loop
# --------------------------------------------------------------------------- #
class App:
    AMP_RATE = 0.9    # keyboard speed, units per second
    FREQ_RATE = 1.6

    def __init__(self, args):
        pygame.init()
        self.args = args
        flags = pygame.SCALED | pygame.RESIZABLE
        if args.fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("Signal Tuner — Model 100")
        pygame.mouse.set_visible(not args.fullscreen)
        self.clock = pygame.time.Clock()
        self.running = True

        self.encoder = None
        if not args.no_gpio:
            try:
                self.encoder = EncoderInput(args.pin_a, args.pin_b, args.invert)
                print("Rotary encoder on GPIO %d/%d controls amplitude." % (args.pin_a, args.pin_b))
            except Exception as exc:  # gpiozero missing, or not running on a Pi
                print("No rotary encoder (%s: %s) - using the keyboard." % (type(exc).__name__, exc))

        self.puzzle = Puzzle()
        hint = ("Knob: amplitude   ← → frequency   1 · 2 · 3 wave   Shift = fast"
                if self.encoder else
                "↑ ↓ amplitude   ← → frequency   1 · 2 · 3 wave   Shift = fast")
        self.ui = Ui(self.puzzle, hint)
        self.puzzle.set_user_shape("sine", time.monotonic())

    # -- input ------------------------------------------------------------- #
    def handle_event(self, e, now):
        p = self.puzzle
        if e.type == pygame.QUIT:
            self.running = False
        elif e.type == pygame.KEYDOWN:
            if e.key in (pygame.K_ESCAPE, pygame.K_q):
                self.running = False
            elif e.key == pygame.K_r:
                p.new_game(now)
            elif e.key == pygame.K_f:
                pygame.display.toggle_fullscreen()
            elif e.key in (pygame.K_1, pygame.K_KP1):
                p.set_user_shape("sine", now)
            elif e.key in (pygame.K_2, pygame.K_KP2):
                p.set_user_shape("square", now)
            elif e.key in (pygame.K_3, pygame.K_KP3):
                p.set_user_shape("saw", now)
        elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
            if self.ui.RESET_BTN.collidepoint(e.pos):
                p.new_game(now)
            for shape, rect in self.ui.user_lamp_hits.items():
                if rect.collidepoint(e.pos):
                    p.set_user_shape(shape, now)
        elif e.type == pygame.MOUSEWHEEL:
            pos = pygame.mouse.get_pos()
            if self.ui.amp_zone.collidepoint(pos):
                p.nudge_amp(0.02 * e.y)
            elif self.ui.freq_zone.collidepoint(pos):
                p.nudge_freq(0.05 * e.y)

    def poll_continuous(self, dt):
        p = self.puzzle
        keys = pygame.key.get_pressed()
        fast = 4 if (keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]) else 1
        p.nudge_amp((keys[pygame.K_UP] - keys[pygame.K_DOWN]) * self.AMP_RATE * fast * dt)
        p.nudge_freq((keys[pygame.K_RIGHT] - keys[pygame.K_LEFT]) * self.FREQ_RATE * fast * dt)
        if self.encoder:
            steps = self.encoder.take()
            if steps:
                p.nudge_amp(steps * self.args.step)

    # -- frame ------------------------------------------------------------- #
    def step(self, now, dt):
        for e in pygame.event.get():
            self.handle_event(e, now)
        self.poll_continuous(dt)
        self.puzzle.update(now)

    def draw(self, now, dt):
        self.ui.render(self.screen, now, dt)
        pygame.display.flip()

    def run(self):
        last = time.monotonic()
        try:
            while self.running:
                now = time.monotonic()
                dt, last = now - last, now
                self.step(now, dt)
                self.draw(now, dt)
                self.clock.tick(self.args.fps)
        finally:
            if self.encoder:
                self.encoder.close()
            pygame.quit()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Signal Tuner - Model 100", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Run with no options for a window; --fullscreen for the escape-room display.")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--no-gpio", action="store_true", help="ignore the rotary encoder, keyboard only")
    ap.add_argument("--invert", action="store_true", help="reverse the knob direction")
    ap.add_argument("--step", type=float, default=0.02, help="amplitude change per encoder click (default 0.02)")
    ap.add_argument("--pin-a", type=int, default=17)
    ap.add_argument("--pin-b", type=int, default=27)
    return ap.parse_args(argv)


if __name__ == "__main__":
    App(parse_args()).run()
