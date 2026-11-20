"""The built-in synthesizer, live: MIDI events in, audio blocks out.

Sixteen channels with program, volume, expression and the sustain pedal, as
a General MIDI synth has, and channel 10 as the drum kit. A note is a
voice: its samples are made once by instruments.py (and kept for the next
time the same note is played), then read a block at a time, so playing is
cheap; the work is in making a note the first time.

A note can come with its length (the `notes` of ovos.midi.events): it is
made at that length, with its own natural release. A plain note_on (a
keyboard, a MIDI file) is made long and released at its note_off.

Events land at the exact sample they are for: `offset` is where in the
next block they start.
"""
from __future__ import annotations

import numpy as np

from .instruments import DRUM_GAIN, FAMILY_GAIN, SR, drum, instrument

DRUMS = 9
#: how long a held note can sound (seconds): sustaining instruments
#: (organ, strings, pads, reeds, brass, leads) and the rest, which die away
HOLD_SUSTAIN, HOLD_DECAY = 8.0, 4.0
SUSTAINING = {2, 5, 6, 7, 8, 9, 10, 11}
RELEASE = {0: 0.25, 1: 0.6, 5: 0.35, 6: 0.35, 11: 0.4}   # seconds, by GM family
POLYPHONY = 96
CACHE_NOTES = 600


class Voice:
    __slots__ = ("ch", "note", "samples", "pos", "gain", "off_at", "rel", "group")

    def __init__(self, ch, note, samples, start, gain, rel, group):
        self.ch, self.note, self.samples = ch, note, samples
        self.pos = -start          # negative: starts that many samples into the block
        self.gain, self.rel, self.group = gain, rel, group
        self.off_at = None         # sample (in the voice) where the release starts


class Reverb:
    """A small room, block by block: convolution with a decaying noise
    impulse response, its tail carried into the next blocks."""

    def __init__(self, sr=SR, seconds=1.1, wet=0.16):
        rng = np.random.default_rng(5)
        n = int(seconds * sr)
        t = np.arange(n) / sr
        ir = rng.standard_normal(n) * np.exp(-t * 6.0 / seconds)
        ir = np.convolve(ir, np.ones(3) / 3, mode="same")
        ir[: int(0.012 * sr)] = 0                       # a little pre-delay
        self.ir = ir / np.sqrt((ir ** 2).sum())
        self.wet = wet
        self.tail = np.zeros(0)
        self._h = {}

    def __call__(self, x):
        n = len(x)
        size = 1 << int(np.ceil(np.log2(n + len(self.ir))))
        h = self._h.get(size)
        if h is None:
            h = self._h[size] = np.fft.rfft(self.ir, size)
        y = np.fft.irfft(np.fft.rfft(x, size) * h, size)[: n + len(self.ir) - 1]
        k = min(len(self.tail), len(y))
        y[:k] += self.tail[:k]
        if len(self.tail) > len(y):
            y = np.concatenate([y, self.tail[len(y):]])
        self.tail = y[n:]
        return x + self.wet * y[:n]


class Synth:
    def __init__(self, sr=SR, gain=0.8, reverb=True):
        self.sr = sr
        self.gain = gain
        self.reverb = Reverb(sr) if reverb else None
        self.voices: list[Voice] = []
        self.cache: dict = {}
        self.reset()

    def reset(self):
        self.program = [0] * 16
        self.volume = [100] * 16
        self.expression = [127] * 16
        self.pedal = [False] * 16
        self.held = set()          # (ch, note, group) let go while the pedal is down

    # ------------------------------------------------------------ notes
    def _samples(self, ch, note, vel, dur):
        if ch == DRUMS:
            key = ("d", note, vel // 8)
        else:
            key = (self.program[ch], note, vel // 8, None if dur is None else round(dur, 2))
        y = self.cache.get(key)
        if y is None:
            if len(self.cache) > CACHE_NOTES:
                self.cache.pop(next(iter(self.cache)))
            if ch == DRUMS:
                y = drum(note, vel) * DRUM_GAIN.get(note, 0.3)
            else:
                prog = self.program[ch]
                fam = prog // 8
                length = dur if dur is not None else (HOLD_SUSTAIN if fam in SUSTAINING else HOLD_DECAY)
                y = instrument(prog, note, length, vel) * FAMILY_GAIN.get(fam, 0.3)
                if dur is None:                 # it ends at its note_off, not by itself
                    tail = int(0.05 * SR)
                    y[-tail:] *= np.linspace(1, 0, tail)
            if self.sr != SR:
                y = np.interp(np.arange(int(len(y) * self.sr / SR)) * SR / self.sr,
                              np.arange(len(y)), y)
            y = self.cache[key] = y.astype(np.float32)
        return y

    def prepare(self, ch, note, vel, dur=None):
        """Make a note's samples ahead of time (they are kept)."""
        self._samples(ch, note, vel, dur)

    def note_on(self, ch, note, vel, offset=0, dur=None, group=None):
        if vel <= 0:
            self.note_off(ch, note, offset, group)
            return
        y = self._samples(ch, note, vel, dur)
        gain = self.volume[ch] / 127 * self.expression[ch] / 127
        rel = RELEASE.get(self.program[ch] // 8, 0.12) if ch != DRUMS else 0.0
        if ch != DRUMS and dur is None:         # the same key again: the old one lets go
            self.note_off(ch, note, offset, group)
        self.voices.append(Voice(ch, note, y, offset, gain, int(rel * self.sr), group))
        if len(self.voices) > POLYPHONY:
            done = [v for v in self.voices if v.off_at is not None] or self.voices
            self.voices.remove(done[0])

    def note_off(self, ch, note, offset=0, group=None):
        if ch == DRUMS:
            return                      # drums ring out
        if self.pedal[ch]:
            self.held.add((ch, note, group))
            return
        for v in self.voices:
            if v.ch == ch and v.note == note and v.off_at is None and v.rel and (
                    group is None or v.group == group):
                v.off_at = max(0, offset - v.pos)

    def program_change(self, ch, prog):
        self.program[ch] = prog & 127

    def control_change(self, ch, ctrl, value, offset=0):
        if ctrl == 7:
            self.volume[ch] = value
        elif ctrl == 11:
            self.expression[ch] = value
        elif ctrl == 64:
            self.pedal[ch] = value >= 64
            if not self.pedal[ch]:
                for c, n, g in [h for h in self.held if h[0] == ch]:
                    self.held.discard((c, n, g))
                    self.note_off(c, n, offset, g)
        elif ctrl in (120, 123):        # all sound off / all notes off
            self.all_off(ch, offset, hard=ctrl == 120)
        elif ctrl == 121:               # reset all controllers
            self.volume[ch], self.expression[ch], self.pedal[ch] = 100, 127, False

    def all_off(self, ch=None, offset=0, hard=False, group=None):
        for v in list(self.voices):
            if (ch is None or v.ch == ch) and (group is None or v.group == group):
                if hard:
                    self.voices.remove(v)
                elif v.off_at is None:
                    v.off_at = max(0, offset - v.pos)
                    v.rel = v.rel or int(0.05 * self.sr)
        if ch is None:
            self.held.clear()
            self.pedal = [False] * 16

    # ------------------------------------------------------------ audio
    def render(self, n: int) -> np.ndarray:
        out = np.zeros(n)
        keep = []
        for v in self.voices:
            a, b = v.pos, v.pos + n
            s0, s1 = max(0, a), min(len(v.samples), b)
            if s1 > s0:
                seg = v.samples[s0:s1].astype(np.float64)
                if v.off_at is not None:
                    j = np.arange(s0, s1)
                    seg = seg * np.clip(1.0 - (j - v.off_at) / max(1, v.rel), 0.0, 1.0)
                out[s0 - a:s1 - a] += seg * v.gain
            v.pos = b
            ended = v.pos >= len(v.samples) or (v.off_at is not None and v.pos >= v.off_at + v.rel)
            if not ended:
                keep.append(v)
        self.voices = keep
        if self.reverb is not None:
            out = self.reverb(out)
        return np.tanh(out * self.gain * 1.2) / np.tanh(1.2)

    @property
    def active(self) -> int:
        return len(self.voices)
