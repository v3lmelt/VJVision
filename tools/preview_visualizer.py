"""Render a reproducible theme preview using synthetic audio and sample tags."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pygame

from vjvision.pastel_visualizer import PastelRenderer
from vjvision.visualizer import VisualState, _pick_font


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/apricot-preview.png")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--cover", default=None)
    parser.add_argument("--standby", action="store_true")
    args = parser.parse_args()
    pygame.font.init()
    surface = pygame.Surface((args.width, args.height))
    renderer = PastelRenderer(_pick_font("auto"))
    state = VisualState(title="花与海的来信 / Letters from the Coast",
                        artist="Apricot Sessions", album="SOUND IN BLOOM",
                        cover_path=args.cover, angle=155)
    details = dict(bpm="164.00", genre="UK Hardcore / UK Garage", duration=276,
                   sample_rate=48000, bit_depth=24, channels=2,
                   bitrate=2307000, format="WAV")
    if args.standby:
        state = VisualState()
        details = {}
    else:
        random = np.random.default_rng(17)
        x = np.linspace(0, 1, 48)
        shape = .22+.52*np.exp(-((x-.2)/.16)**2)+.23*np.exp(-((x-.55)/.14)**2)
        for frame in range(300):
            t = frame/10
            bins = shape*(.85+.12*np.sin(x*24+t))+random.uniform(-.09, .09, 48)
            renderer.observe(dict(bins=np.clip(bins, 0, 1), rms=.14+.06*np.sin(t*1.7)**2,
                                  input_peak=.51, sample_rate=48000, channels=2), now=t)
        for i in range(30):
            renderer.draw(surface, state, details, now=29.9+i/60)
    renderer.draw(surface, state, details, now=30.4)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(surface, output)
    print(output.resolve())
    pygame.quit()


if __name__ == "__main__":
    main()
