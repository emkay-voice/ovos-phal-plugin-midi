"""FluidSynth as the instrument: real instrument samples from a SoundFont.

FluidSynth makes the samples here, a block at a time (pyfluidsynth's
get_samples), and the engine mixes them with clips, ducks them and sends
them to the same output as the built-in synth. So FluidSynth's own audio
driver is not used, and everything sounds from one place, in time.

The same calls as synth.Synth: events land at their sample in the block.
"""
from __future__ import annotations

import numpy as np

DRUMS = 9


class FluidSynth:
    def __init__(self, soundfont: str, sr: int = 24000, gain: float = 0.5, reverb: bool = True):
        import fluidsynth
        self.sr = sr
        self.fs = fluidsynth.Synth(gain=gain, samplerate=float(sr))
        try:
            self.fs.set_reverb(roomsize=0.4, damping=0.3, width=0.8, level=0.35 if reverb else 0.0)
        except Exception:
            pass
        self.sfid = self.fs.sfload(soundfont)
        if self.sfid < 0:
            raise RuntimeError(f"could not load the SoundFont {soundfont}")
        self.soundfont = soundfont
        self.reset()
        self._queue: list = []          # (offset, n, fn, args) for the next block
        self._ending: list = []         # [samples left, ch, note, group] notes with a length
        self._groups: dict = {}         # group -> {(ch, note)}
        self._seq = 0

    def reset(self):
        for ch in range(16):
            self.fs.program_select(ch, self.sfid, 128 if ch == DRUMS else 0, 0)

    def _at(self, offset, fn, *args):
        self._seq += 1
        self._queue.append((offset, self._seq, fn, args))

    # ------------------------------------------------------------ the calls
    def prepare(self, *a, **k):
        pass                            # FluidSynth needs no preparing

    def note_on(self, ch, note, vel, offset=0, dur=None, group=None):
        if vel <= 0:
            return self.note_off(ch, note, offset, group)
        self._at(offset, self.fs.noteon, ch, note, vel)
        self._groups.setdefault(group, set()).add((ch, note))
        if dur is not None:
            self._ending.append([offset + int(dur * self.sr), ch, note, group])

    def note_off(self, ch, note, offset=0, group=None):
        self._at(offset, self.fs.noteoff, ch, note)
        self._groups.get(group, set()).discard((ch, note))

    def program_change(self, ch, prog):
        self._at(0, self.fs.program_select, ch, self.sfid, 128 if ch == DRUMS else 0, prog & 127)

    def control_change(self, ch, ctrl, value, offset=0):
        self._at(offset, self.fs.cc, ch, ctrl, value)

    def all_off(self, ch=None, offset=0, hard=False, group=None):
        if group is not None:
            for c, n in list(self._groups.pop(group, set())):
                if ch is None or c == ch:
                    self._at(offset, self.fs.noteoff, c, n)
            self._ending = [e for e in self._ending if e[3] != group]
            return
        for c in (range(16) if ch is None else [ch]):
            self._at(offset, self.fs.cc, c, 120 if hard else 123, 0)
        self._ending = [e for e in self._ending if ch is not None and e[1] != ch]

    # ------------------------------------------------------------ audio
    def render(self, n: int) -> np.ndarray:
        for e in self._ending:          # notes with a length that end in this block
            if e[0] < n:
                self._at(max(0, e[0]), self.fs.noteoff, e[1], e[2])
                self._groups.get(e[3], set()).discard((e[1], e[2]))
        self._ending = [[e[0] - n, *e[1:]] for e in self._ending if e[0] >= n]
        queue = sorted(self._queue)
        self._queue = []
        out, pos = [], 0
        for offset, _, fn, args in queue:
            offset = min(max(offset, pos), n)
            if offset > pos:
                out.append(self._samples(offset - pos))
                pos = offset
            fn(*args)
        if pos < n:
            out.append(self._samples(n - pos))
        return np.concatenate(out) if out else np.zeros(n)

    def _samples(self, k):
        s = np.asarray(self.fs.get_samples(k), dtype=np.float64).reshape(-1, 2)
        return s.mean(axis=1) / 32768.0

    @property
    def active(self) -> int:
        return sum(len(v) for v in self._groups.values()) + len(self._ending) + (1 if self._queue else 0)

    def close(self):
        try:
            self.fs.delete()
        except Exception:
            pass
