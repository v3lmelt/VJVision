"""Apricot instrument-panel theme. All meters consume live capture data.

The spectrum is relative amplitude, not calibrated dB. The history represents
the last 30 seconds of input, not playback position in a recognized file.
"""
from __future__ import annotations

from collections import deque
from functools import lru_cache
import logging
import math
import time

import numpy as np
import pygame

log = logging.getLogger(__name__)

BG = (252, 233, 213)
PAPER = (255, 242, 224)
PINK = (203, 111, 145)
INK = (167, 87, 111)
MUTED = (175, 132, 121)
GRID = (237, 209, 190)
BLUE = (126, 174, 207)
LIGHT_PINK = (235, 169, 184)


def _db(value: float) -> str:
    return f"{20 * math.log10(value):.1f}" if value > 0.00001 else "−∞"


def _duration(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}" if seconds > 0 else "—"


class PastelRenderer:
    """Cache artwork and type; animate bars, orbital ticks and input history."""

    def __init__(self, font_name: str):
        self.font_name = font_name
        self.history: deque[tuple[float, float]] = deque(maxlen=600)
        self.target = np.zeros(128)
        self.levels = np.zeros(128)
        self.rms = self.input_peak = 0.0
        self.sample_rate = self.channels = 0
        self.last_input = self.last_history = -100.0
        self.last_frame: float | None = None
        self.art_key = None
        self.art = None
        self.old_art = None
        self.art_changed = 0.0
        self.background = None
        self.canvas = None

    def set_font(self, name: str) -> None:
        if self.font_name != name:
            self.font_name = name
            self._font.cache_clear()
            self._label.cache_clear()

    @lru_cache(maxsize=32)
    def _font(self, size: int, mono: bool = False):
        return pygame.font.SysFont("consolas" if mono else self.font_name, size)

    @lru_cache(maxsize=384)
    def _label(self, value: str, size: int, color: tuple, mono: bool):
        return self._font(size, mono).render(value, True, color)

    def text(self, canvas, value, x, y, size=16, color=MUTED,
             mono=False, width=None, align="left"):
        value = str(value)
        font = self._font(size, mono)
        if width is not None and font.size(value)[0] > width:
            while value and font.size(value + "…")[0] > width:
                value = value[:-1]
            value += "…"
        label = self._label(value, size, color, mono)
        if align == "right":
            x -= label.get_width()
        elif align == "center":
            x -= label.get_width() // 2
        canvas.blit(label, (int(x), int(y)))

    def observe(self, message: dict, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        raw = np.nan_to_num(np.asarray(message.get("bins", []), dtype=float),
                            nan=0.0, posinf=1.0, neginf=0.0).ravel()
        if raw.size:
            self.target = np.clip(np.interp(np.linspace(0, 1, 128),
                                            np.linspace(0, 1, raw.size), raw), 0, 1)
        self.rms = float(np.clip(message.get("rms", message.get("peak", 0)), 0, 1))
        self.input_peak = float(np.clip(message.get("input_peak", self.rms), 0, 1))
        self.sample_rate = int(message.get("sample_rate", 0))
        self.channels = int(message.get("channels", 0))
        self.last_input = now
        if now - self.last_history >= 0.1:
            self.history.append((now, self.rms))
            self.last_history = now

    def reset(self) -> None:
        self.history.clear()
        self.art_key = None
        self.art = self.old_art = None

    def _background(self, size):
        if self.background is not None and self.background.get_size() == size:
            return self.background
        bg = pygame.Surface(size)
        bg.fill(BG)
        w, h = size
        # Fine graph-paper texture remains quiet behind the instrumentation.
        for x in range(0, w, 32):
            pygame.draw.line(bg, (249, 227, 207), (x, 0), (x, h))
        for y in range(0, h, 32):
            pygame.draw.line(bg, (249, 227, 207), (0, y), (w, y))
        for x, y, dx, dy in ((24, 24, 1, 1), (w-24, 24, -1, 1),
                             (24, h-24, 1, -1), (w-24, h-24, -1, -1)):
            pygame.draw.lines(bg, GRID, False,
                              [(x+dx*20, y), (x, y), (x, y+dy*20)], 1)
        self.background = bg
        return bg

    def _artwork(self, path, side: int, now: float):
        key = (path, side)
        if key != self.art_key:
            self.old_art = self.art
            self.art_changed = now
            self.art_key = key
            self.art = None
            if path:
                try:
                    source = pygame.image.load(path)
                    edge = min(source.get_size())
                    crop = source.subsurface(source.get_rect().inflate(
                        edge-source.get_width(), edge-source.get_height()))
                    self.art = pygame.transform.smoothscale(crop, (side, side))
                except (OSError, pygame.error, ValueError) as exc:
                    log.warning("Pastel cover unavailable: %s", exc)
            if self.art is None:
                self.art = pygame.Surface((side, side))
                self.art.fill((248, 215, 198))
                for i in range(9):
                    radius = int(side * (0.18 + i * 0.044))
                    pygame.draw.circle(self.art, (234, 180, 172),
                                       (int(side*.78), int(side*.25)), radius, 1)
                pygame.draw.circle(self.art, (226, 158, 166),
                                   (int(side*.78), int(side*.25)), int(side*.13))
                pygame.draw.line(self.art, BLUE, (int(side*.10), int(side*.68)),
                                 (int(side*.9), int(side*.68)), 2)
                self.text(self.art, "音", side*.08, side*.16, int(side*.30), INK)
                self.text(self.art, "SOUND IN BLOOM", side*.10, side*.75,
                          max(12, int(side*.047)), INK, True)
                self.text(self.art, "VJ / VISUAL", side*.10, side*.84,
                          max(10, int(side*.031)), MUTED, True)
        return self.art

    def _cover(self, c, center, side, state, standby_path, now):
        cx, cy = center
        r = side * .72
        for factor in (1.0, 1.13, 1.25):
            pygame.draw.circle(c, GRID, center, int(r*factor), 1)
        for tick in range(120):
            angle = math.tau * tick / 120
            radius = r * 1.13
            length = 10 if tick % 10 == 0 else 4
            p1 = (cx + math.cos(angle)*radius, cy + math.sin(angle)*radius)
            p2 = (cx + math.cos(angle)*(radius-length), cy + math.sin(angle)*(radius-length))
            pygame.draw.line(c, LIGHT_PINK if tick % 10 == 0 else GRID, p1, p2)
        for offset in (0, 120, 240):
            angle = math.radians(state.angle*.12 + offset)
            pygame.draw.circle(c, BLUE if offset == 0 else LIGHT_PINK,
                               (int(cx+math.cos(angle)*r), int(cy+math.sin(angle)*r)), 3)
        for text, x, y in (("000", cx, cy-r*1.25-22),
                           ("090", cx+r*1.25+17, cy-7),
                           ("270", cx-r*1.25-17, cy-7)):
            self.text(c, text, x, y, 12, PINK, True, align="center")
        art = self._artwork(state.cover_path or (standby_path if not state.title else None), side, now)
        rect = pygame.Rect(0, 0, side, side)
        rect.center = center
        pygame.draw.rect(c, GRID, rect.move(7, 9))
        pygame.draw.rect(c, PAPER, rect.inflate(16, 16))
        c.blit(art, rect)
        if self.old_art is not None and now-self.art_changed < .65:
            old = pygame.transform.smoothscale(self.old_art, (side, side))
            old.set_alpha(int(255 * max(0, 1-(now-self.art_changed)/.65)))
            c.blit(old, rect)
        else:
            self.old_art = None
        for x, y, dx, dy in ((rect.left-10, rect.top-10, 1, 1),
                             (rect.right+10, rect.top-10, -1, 1),
                             (rect.left-10, rect.bottom+10, 1, -1),
                             (rect.right+10, rect.bottom+10, -1, -1)):
            pygame.draw.lines(c, BLUE, False,
                              [(x+dx*27, y), (x, y), (x, y+dy*27)], 3)
        self.text(c, "ARTWORK / 01", cx, rect.top-42, 13, MUTED, True, align="center")
        self.text(c, state.album or "SOUND IN BLOOM", cx, rect.bottom+28,
                  15, INK, width=side+40, align="center")
        indicator_y = int(cy+r*1.25+37)
        pygame.draw.circle(c, GRID, (cx, indicator_y), 28, 1)
        pygame.draw.circle(c, LIGHT_PINK, (cx, indicator_y), 21)
        for i in range(5):
            height = 5 + int(min(1, self.rms*5) * (14-abs(i-2)*4))
            x = cx - 12 + i*6
            pygame.draw.line(c, PAPER, (x, indicator_y-height//2),
                             (x, indicator_y+height//2), 2)
        self.text(c, "AUDIO", cx-52, indicator_y-7, 12, MUTED, True, align="right")
        self.text(c, "REACTIVE", cx+52, indicator_y-7, 12, MUTED, True)

    def _title(self, c, value, x, y, width):
        size = 44
        while size > 30 and self._font(size).size(value)[0] > width*1.85:
            size -= 2
        font = self._font(size)
        line = ""
        rest = value
        while rest and font.size(line+rest[0])[0] <= width:
            line, rest = line+rest[0], rest[1:]
        self.text(c, line, x, y, size, PINK)
        if rest:
            self.text(c, rest, x, y+size+6, size, PINK, width=width)

    def _spectrum(self, c, rect, style):
        x, y, w, h = rect
        self.text(c, "FREQUENCY / Hz", x, y-35, 12, MUTED, True)
        self.text(c, "RELATIVE AMPLITUDE", x+w, y-35, 12, MUTED, True, align="right")
        for i in range(5):
            yy = y+h*i/4
            pygame.draw.line(c, GRID, (x, yy), (x+w, yy))
            self.text(c, str(100-i*25), x-12, yy-7, 11, MUTED, True, align="right")
        maximum = max(100, (self.sample_rate or 44100)/2)
        mel_min, mel_max = np.log10(1+80/700), np.log10(1+maximum/700)
        for freq, label in ((80, "80"), (200, "200"), (1000, "1k"),
                            (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")):
            if freq > maximum:
                continue
            xx = x+w*(np.log10(1+freq/700)-mel_min)/(mel_max-mel_min)
            for yy in range(y, y+h, 9):
                pygame.draw.line(c, GRID, (xx, yy), (xx, yy+2))
            self.text(c, label, xx, y+h+11, 11, MUTED, True, align="center")
        if style == "wave":
            points = [(x+i*w/127, y+h-v*h*.9) for i, v in enumerate(self.levels)]
            pygame.draw.polygon(c, LIGHT_PINK, [(x, y+h), *points, (x+w, y+h)])
            pygame.draw.aalines(c, PINK, False, points)
        else:
            step = w/len(self.levels)
            for i, value in enumerate(self.levels):
                height = max(1, int(value*h*.9))
                top = y+h//2-height//2 if style == "mirror" else y+h-height
                pygame.draw.rect(c, PINK, (x+i*step, top, max(1, step-2), height))
                if height > 4:
                    pygame.draw.line(c, LIGHT_PINK, (x+i*step, top),
                                     (x+i*step+max(1, step-2), top))
        pygame.draw.line(c, PINK, (x, y+h), (x+w, y+h))

    def _history(self, c, rect, now):
        x, y, w, h = rect
        self.text(c, "INPUT HISTORY / 30 s", x, y-27, 12, MUTED, True)
        self.text(c, "NOW", x+w, y-27, 12, BLUE, True, align="right")
        pygame.draw.rect(c, GRID, rect, 1)
        # Empty and dropped-input intervals stay blank, never fabricated.
        for timestamp, value in self.history:
            age = now-timestamp
            if 0 <= age <= 30:
                xx = x+w*(1-age/30)
                height = int(np.clip(value**.5, 0, 1)*(h-6))
                if height:
                    pygame.draw.line(c, LIGHT_PINK, (xx, y+h//2-height//2),
                                     (xx, y+h//2+height//2), max(1, int(w/300)))
        pygame.draw.line(c, BLUE, (x+w, y-4), (x+w, y+h+4), 2)
        for i in range(4):
            self.text(c, f"−{30-i*10}s" if i < 3 else "0s", x+w*i/3,
                      y+h+7, 10, MUTED, True, align="right" if i == 3 else "left")

    def draw(self, screen, state, details=None, standby_path="", now=None):
        now = time.monotonic() if now is None else now
        dt = min(.1, max(0, now-self.last_frame)) if self.last_frame is not None else 1/60
        self.last_frame = now
        fresh = now-self.last_input < .75
        target = self.target if fresh else np.zeros_like(self.target)
        self.levels += (target-self.levels)*(1-math.exp(-dt*14))
        if not fresh:
            self.rms = self.input_peak = 0.0
        while self.history and now-self.history[0][0] > 30:
            self.history.popleft()
        portrait = screen.get_width()/max(1, screen.get_height()) < 1.15
        size = (1000, 1500) if portrait else (1600, 900)
        if self.canvas is None or self.canvas.get_size() != size:
            self.canvas = pygame.Surface(size)
        c = self.canvas
        c.blit(self._background(size), (0, 0))
        w, h = size
        self.text(c, "VJ / VISUAL", 64, 40, 17, INK, True)
        self.text(c, "S O U N D   I N   B L O O M", w-64, 42, 12, MUTED, True, align="right")
        pygame.draw.line(c, GRID, (64, 76), (w-64, 76))
        center = (500, 350) if portrait else (326, 423)
        side = 280 if portrait else 330
        self._cover(c, center, side, state, standby_path, now)
        x, top, width = (80, 690, 840) if portrait else (650, 115, 870)
        pygame.draw.rect(c, BLUE, (x, top+6, 6, 6))
        self.text(c, "NOW PLAYING" if state.title else "LIVE AUDIO VISUALIZER",
                  x+18, top, 13, MUTED, True)
        self._title(c, state.title or "让声音，绽放成画面", x, top+30, width)
        self.text(c, state.artist or ("等待曲目识别" if state.title else "选择音频输入 · 开始你的音乐现场"),
                  x, top+134, 22, INK, width=width)
        data = details or {}
        metadata_y = top+193
        pygame.draw.line(c, GRID, (x, metadata_y-17), (x+width, metadata_y-17))
        for label, value, px, span in (
            ("BPM / TAG", data.get("bpm") or "—", x, 180),
            ("GENRE", data.get("genre") or "—", x+215, width-360),
            ("DURATION", _duration(data.get("duration", 0)), x+width-115, 115)):
            self.text(c, label, px, metadata_y, 12, MUTED, True)
            self.text(c, value, px, metadata_y+23, 23, PINK, width=span)
        spectrum_y = top+292
        self._spectrum(c, (x, spectrum_y, width, 203), state.style)
        self._history(c, (x, spectrum_y+272, width, 48), now)
        metrics_y = spectrum_y+350
        for label, value, px in (("INPUT RMS", _db(self.rms) if fresh else "—", x),
                                 ("INPUT PEAK", _db(self.input_peak) if fresh else "—", x+215)):
            self.text(c, label, px, metrics_y, 12, MUTED, True)
            self.text(c, value, px, metrics_y+21, 31, PINK, True)
            self.text(c, "dBFS", px+145, metrics_y+38, 11, MUTED, True)
            pygame.draw.line(c, GRID, (px, metrics_y+67), (px+180, metrics_y+67), 2)
            v = self.rms if px == x else self.input_peak
            meter = max(0, min(1, (20*math.log10(max(v, 1e-6))+60)/60))
            pygame.draw.line(c, BLUE, (px, metrics_y+67), (px+int(180*meter), metrics_y+67), 3)
        rate = data.get("sample_rate", 0)
        bits = data.get("bit_depth", 0)
        channels = data.get("channels", 0)
        bitrate = data.get("bitrate", 0)
        values = [("FILE RATE", f"{rate/1000:g} kHz" if rate else "—"),
                  ("BIT DEPTH", f"{bits} bit" if bits else "—"),
                  ("CHANNELS", {1: "Mono", 2: "Stereo"}.get(channels, str(channels) if channels else "—")),
                  ("BITRATE", f"{bitrate/1000:.0f} kbps" if bitrate else "—"),
                  ("FORMAT", data.get("format") or "—"),
                  ("INPUT RATE", f"{self.sample_rate/1000:g} kHz" if self.sample_rate else "—")]
        for i, (label, value) in enumerate(values):
            px = x+460 + (i % 3)*(width-460)/3
            py = metrics_y + (i//3)*47
            self.text(c, label, px, py, 10, MUTED, True)
            self.text(c, value, px, py+17, 15, PINK, True)
        pygame.draw.line(c, GRID, (64, h-61), (w-64, h-61))
        self.text(c, "杏桃频谱  /  APRICOT SESSION", 64, h-44, 12, INK)
        self.text(c, "LIVE INPUT  ·  30 SECOND MEMORY", w-64, h-43, 11, MUTED, True, align="right")
        # Keep typography and geometry undistorted at every window aspect.
        scale = min(screen.get_width()/w, screen.get_height()/h)
        output_size = (max(1, round(w*scale)), max(1, round(h*scale)))
        screen.fill(BG)
        output = c if output_size == size else pygame.transform.smoothscale(c, output_size)
        screen.blit(output, output.get_rect(center=screen.get_rect().center))
