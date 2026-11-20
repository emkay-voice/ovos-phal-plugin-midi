"""What plays the events: a timeline, and three engines.

Events are timed in wall-clock seconds (time.time()), so a skill can send a
bar of music a moment ahead and it plays exactly then, however late the
message arrived (up to the engine's latency). Each event belongs to a
`source` (the skill or file that sent it), so a source's future events can
be taken back (ovos.midi.cancel) - a skill changing its mind mid-song
without a gap - and its sounding notes stopped without touching anyone
else's.

  BuiltinEngine   the numpy synth (synth.py) to an audio output (audio.py)
  FluidSynthEngine  FluidSynth through pyfluidsynth, with a SoundFont
  PortEngine      a MIDI output port: a USB keyboard, a synth, a drum module
"""
from __future__ import annotations

import heapq
import itertools
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .synth import Synth

NOTE_OFF_GRACE = 0.002     # a note_off and the next note_on at the same time: off first


@dataclass(order=True)
class Event:
    t: float                              # wall clock, seconds
    seq: int
    kind: str = field(compare=False)      # on, off, program, cc, all_off
    ch: int = field(compare=False, default=0)
    a: int = field(compare=False, default=0)   # note / program / controller
    b: int = field(compare=False, default=0)   # velocity / value
    dur: float | None = field(compare=False, default=None)
    source: str = field(compare=False, default="")


class Timeline:
    """Events waiting for their time, safe to add to from any thread."""

    def __init__(self):
        self._heap: list[Event] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self.added = threading.Event()

    def add(self, t, kind, ch=0, a=0, b=0, dur=None, source=""):
        if kind == "off":
            t -= NOTE_OFF_GRACE
        ev = Event(t, next(self._seq), kind, ch, a, b, dur, source)
        with self._lock:
            heapq.heappush(self._heap, ev)
        self.added.set()
        return ev

    def due(self, until: float) -> list[Event]:
        out = []
        with self._lock:
            while self._heap and self._heap[0].t < until:
                out.append(heapq.heappop(self._heap))
        return out

    def next_time(self):
        with self._lock:
            return self._heap[0].t if self._heap else None

    def cancel(self, source: str, after: float = 0.0) -> int:
        """Drop `source`'s events from `after` on; returns how many."""
        with self._lock:
            keep = [e for e in self._heap if not (e.source == source and e.t >= after)]
            n = len(self._heap) - len(keep)
            heapq.heapify(keep)
            self._heap = keep
        return n

    def upcoming(self, until: float) -> list[Event]:
        with self._lock:
            return [e for e in self._heap if e.t < until]

    def clear(self):
        with self._lock:
            self._heap = []


class Engine:
    name = "none"
    sample_rate = 0
    latency = 0.0          # schedule at least this far ahead to be on time

    def __init__(self):
        self.timeline = Timeline()
        self._stop = threading.Event()
        self._thread = None
        self.sounding: dict[str, set] = {}   # source -> {(ch, note)} it has on

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"midi-{self.name}", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self.timeline.added.set()
        if self._thread:
            self._thread.join(2)

    def add(self, *args, **kw):
        return self.timeline.add(*args, **kw)

    def stop_source(self, source: str):
        """Its future events dropped, its notes let go."""
        self.timeline.cancel(source)
        now = time.time()
        for ch, note in self.sounding.pop(source, set()):
            self.timeline.add(now, "off", ch, note, source=source)

    def panic(self):
        self.timeline.clear()
        self.sounding.clear()
        now = time.time()
        for ch in range(16):
            self.timeline.add(now, "cc", ch, 123, 0)

    def _track(self, ev: Event):
        if ev.kind == "on" and ev.b > 0:
            self.sounding.setdefault(ev.source, set()).add((ev.ch, ev.a))
        elif ev.kind == "off" or (ev.kind == "on" and ev.b == 0):
            self.sounding.get(ev.source, set()).discard((ev.ch, ev.a))

    def _run(self):
        """Dispatch each event at its time (for engines that play by themselves)."""
        while not self._stop.is_set():
            nxt = self.timeline.next_time()
            wait = 0.5 if nxt is None else nxt - time.time()
            if wait > 0.0005:
                self.timeline.added.clear()
                self.timeline.added.wait(min(wait, 0.5))
                continue
            for ev in self.timeline.due(time.time() + 0.0005):
                self._track(ev)
                try:
                    self.send(ev)
                except Exception:
                    pass

    def send(self, ev: Event):
        pass

    def status(self) -> dict:
        return {"engine": self.name, "sample_rate": self.sample_rate, "latency": self.latency}


class BuiltinEngine(Engine):
    """The numpy synth, a block at a time to an audio output."""
    name = "builtin"

    def __init__(self, sink, block=1024, gain=0.8, reverb=True):
        super().__init__()
        self.sink = sink
        self.block = block
        self.sample_rate = sink.sample_rate
        self.synth = Synth(sink.sample_rate, gain=gain, reverb=reverb)
        self.latency = sink.latency + block / sink.sample_rate
        self.t0 = None               # wall time at which sample 0 is heard
        self.produced = 0
        self.idle_blocks = 0

    def _prepare_ahead(self, until):
        """Make the samples of notes coming soon, before they are due."""
        for ev in self.timeline.upcoming(until):
            if ev.kind == "on" and ev.b > 0 and ev.ch != 9 and ev.dur is not None:
                self.synth.prepare(ev.ch, ev.a, ev.b, ev.dur)

    def stop_source(self, source: str):
        super().stop_source(source)
        self.timeline.add(time.time(), "all_off", -1, source=source)   # its notes with a length, too

    def _apply(self, ev: Event, offset: int):
        s = self.synth
        self._track(ev)
        if ev.kind == "on":
            s.note_on(ev.ch, ev.a, ev.b, offset, ev.dur, ev.source)
        elif ev.kind == "off":
            s.note_off(ev.ch, ev.a, offset, ev.source)
        elif ev.kind == "program":
            s.program_change(ev.ch, ev.a)
        elif ev.kind == "cc":
            s.control_change(ev.ch, ev.a, ev.b, offset)
        elif ev.kind == "all_off":
            s.all_off(None if ev.ch < 0 else ev.ch, offset, group=ev.source or None)

    def start(self):
        if sys.platform == "emscripten":
            # a browser (Emkay): one thread, and a sleep may not really
            # sleep - so no loop; a timer makes what is due, a block at a time
            self.sink.open()
            self._tick()
        else:
            super().start()

    def shutdown(self):
        super().shutdown()
        if sys.platform == "emscripten":
            self.sink.close()

    def _tick(self):
        if self._stop.is_set():
            return
        sr, n = self.sample_rate, self.block
        now = time.time()
        if self.t0 is None:
            if self.timeline.next_time() is not None:
                self.t0 = now + self.latency
                self.produced = 0
        elif self.synth.active == 0 and self.timeline.next_time() is None:
            self.t0 = None
        while self.t0 is not None:
            start = self.t0 + self.produced / sr
            if start > now + self.latency:
                break
            if start < now - 0.5:           # fell far behind (the page was busy): catch up
                self.produced += int((now - start) * sr) // n * n
                continue
            self._block(start, n)
        t = threading.Timer(n / sr / 2, self._tick)
        t.daemon = True
        t.start()

    def _block(self, start, n):
        sr = self.sample_rate
        end = start + n / sr
        self._prepare_ahead(end + 0.5)
        for ev in self.timeline.due(end):
            self._apply(ev, min(n - 1, max(0, int(round((ev.t - start) * sr)))))
        x = self.synth.render(n)
        self.sink.write((np.clip(x, -1, 1) * 32767).astype(np.int16), start)
        self.produced += n

    def _run(self):
        sr, n = self.sample_rate, self.block
        self.sink.open()
        try:
            while not self._stop.is_set():
                if self.t0 is None or (self.synth.active == 0 and self.timeline.next_time() is None):
                    # nothing to play: keep quiet (and the output idle) until something comes
                    self.idle_blocks += 1
                    if self.t0 is not None and self.idle_blocks > sr // n * 2:
                        self.sink.pause()
                        self.t0 = None
                    if self.t0 is None:
                        self.timeline.added.clear()
                        if self.timeline.next_time() is None:
                            self.timeline.added.wait(0.25)
                            continue
                        self.sink.resume()
                        self.t0 = time.time() + self.latency
                        self.produced = 0
                else:
                    self.idle_blocks = 0
                start = self.t0 + self.produced / sr
                if not self.sink.paced:     # an output that doesn't hold us back: keep time ourselves
                    ahead = start - time.time() - self.sink.latency
                    if ahead > 0:
                        time.sleep(ahead)
                self._block(start, n)
        finally:
            self.sink.close()

    def status(self):
        return {**super().status(), "output": self.sink.name, "voices": self.synth.active}


class FluidSynthEngine(Engine):
    """FluidSynth with a SoundFont (real instrument samples), via pyfluidsynth."""
    name = "fluidsynth"

    def __init__(self, soundfont, driver=None, gain=0.5, sample_rate=44100):
        super().__init__()
        import fluidsynth
        self.fs = fluidsynth.Synth(gain=gain, samplerate=float(sample_rate))
        self.fs.start(driver=driver) if driver else self.fs.start()
        self.sfid = self.fs.sfload(soundfont)
        for ch in range(16):
            self.fs.program_select(ch, self.sfid, 128 if ch == 9 else 0, 0)
        self.soundfont = soundfont
        self.sample_rate = sample_rate
        self.latency = 0.05

    def send(self, ev: Event):
        fs = self.fs
        if ev.kind == "on":
            fs.noteon(ev.ch, ev.a, ev.b)
            if ev.dur is not None:
                self.timeline.add(ev.t + ev.dur, "off", ev.ch, ev.a, source=ev.source)
        elif ev.kind == "off":
            fs.noteoff(ev.ch, ev.a)
        elif ev.kind == "program":
            fs.program_select(ev.ch, self.sfid, 128 if ev.ch == 9 else 0, ev.a)
        elif ev.kind == "cc":
            fs.cc(ev.ch, ev.a, ev.b)
        elif ev.kind == "all_off":
            for ch in (range(16) if ev.ch < 0 else [ev.ch]):
                fs.cc(ch, 123, 0)

    def shutdown(self):
        super().shutdown()
        try:
            self.fs.delete()
        except Exception:
            pass

    def status(self):
        return {**super().status(), "soundfont": self.soundfont}


class PortEngine(Engine):
    """Out to a real MIDI instrument (mido; python-rtmidi for the ports)."""
    name = "port"

    def __init__(self, port_name):
        super().__init__()
        import mido
        self.mido = mido
        self.port = mido.open_output(port_name)
        self.port_name = port_name
        self.latency = 0.01

    def send(self, ev: Event):
        m = self.mido.Message
        if ev.kind == "on":
            self.port.send(m("note_on", channel=ev.ch, note=ev.a, velocity=ev.b))
            if ev.dur is not None:
                self.timeline.add(ev.t + ev.dur, "off", ev.ch, ev.a, source=ev.source)
        elif ev.kind == "off":
            self.port.send(m("note_off", channel=ev.ch, note=ev.a))
        elif ev.kind == "program":
            self.port.send(m("program_change", channel=ev.ch, program=ev.a))
        elif ev.kind == "cc":
            self.port.send(m("control_change", channel=ev.ch, control=ev.a, value=ev.b))
        elif ev.kind == "all_off":
            for ch in (range(16) if ev.ch < 0 else [ev.ch]):
                self.port.send(m("control_change", channel=ch, control=123, value=0))

    def shutdown(self):
        super().shutdown()
        self.port.close()

    def status(self):
        return {**super().status(), "port": self.port_name}
