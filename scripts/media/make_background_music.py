#!/usr/bin/env python3
"""Synthesize an original, quiet ambient background track for the walkthrough video.

Everything is generated here from sine partials (no samples, no third-party
audio), so the track carries no licensing questions. Chord pads in A minor
(Am - F - C - G), a soft sub bass and a light arpeggio, with fade in and out.

Usage:
  python3 scripts/media/make_background_music.py --seconds 163 --out runtime-data/walkthrough/music.wav
"""

from __future__ import annotations

import argparse
import wave

import numpy as np

RATE = 44_100
BPM = 84
BEAT = 60 / BPM
BAR = 4 * BEAT

# Chord tones as MIDI notes: Am, F, C, G (two bars each).
CHORDS = [
    (57, 60, 64, 69),
    (53, 57, 60, 65),
    (48, 55, 60, 64),
    (55, 59, 62, 67),
]


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def pad(freq: float, n: int, t: np.ndarray) -> np.ndarray:
    # Two slightly detuned voices with a few soft harmonics and a slow swell.
    out = np.zeros(n)
    for detune in (-0.12, 0.12):
        f = hz(freq) * 2 ** (detune * 8 / 1200)  # about +/-1 cent
        for k, amp in ((1, 1.0), (2, 0.25), (3, 0.08)):
            out += amp * np.sin(2 * np.pi * f * k * t)
    env = np.minimum(1.0, t / 1.6) * np.minimum(1.0, (t[-1] - t) / 1.6 + 1e-9)
    return out * env


def pluck(freq: float, n: int) -> np.ndarray:
    t = np.arange(n) / RATE
    f = hz(freq)
    tone = np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t)
    return tone * np.exp(-t * 5.5) * np.minimum(1.0, t / 0.005)


def reverb(x: np.ndarray, seconds: float = 2.2, mix: float = 0.28) -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(seconds * RATE)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / RATE * 3.2)
    ir /= np.sqrt(np.sum(ir**2))
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    wet = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x)]
    return (1 - mix) * x + mix * wet


def render(seconds: float) -> np.ndarray:
    total = int(seconds * RATE)
    mix = np.zeros(total)
    seg = int(2 * BAR * RATE)
    i = 0
    while i * seg < total:
        start = i * seg
        n = min(seg + int(1.0 * RATE), total - start)  # overlap the next chord slightly
        t = np.arange(n) / RATE
        chord = CHORDS[i % len(CHORDS)]
        for note in chord:
            mix[start:start + n] += 0.07 * pad(note, n, t)
        bass_env = np.minimum(1.0, t / 0.8) * np.minimum(1.0, (t[-1] - t) / 1.0)
        mix[start:start + n] += 0.10 * np.sin(2 * np.pi * hz(chord[0] - 12) * t) * bass_env
        # Eighth-note arpeggio, entering after the first chord cycle.
        if i >= 1:
            step = int(BEAT / 2 * RATE)
            pattern = [chord[0] + 12, chord[1] + 12, chord[2] + 12, chord[3] + 12, chord[2] + 12, chord[1] + 12]
            for j in range(seg // step):
                s = start + j * step
                ln = min(int(0.9 * RATE), total - s)
                if ln <= 0:
                    break
                mix[s:s + ln] += 0.035 * pluck(pattern[j % len(pattern)], ln)
        i += 1
    mix = reverb(mix)
    fade_in, fade_out = int(2.0 * RATE), int(4.0 * RATE)
    mix[:fade_in] *= np.linspace(0, 1, fade_in)
    mix[-fade_out:] *= np.linspace(1, 0, fade_out)
    return mix / (np.max(np.abs(mix)) + 1e-9) * 0.7


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    left = render(args.seconds)
    # A tiny delay on the right channel gives width without changing the mix.
    right = np.concatenate([np.zeros(int(0.012 * RATE)), left])[: len(left)]
    pcm = (np.stack([left, right], axis=1) * 32767).astype("<i2")
    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f"Wrote {args.out} ({args.seconds:.1f} s)")


if __name__ == "__main__":
    main()
