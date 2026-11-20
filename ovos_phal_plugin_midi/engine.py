"""What plays the events: a timeline, the mixer, and the MIDI port.

Events are timed in wall-clock seconds (time.time()), so a skill can send a
bar of music a moment ahead and it plays exactly then, however late the
message arrived (up to the engine's latency). Each event belongs to a
`source` (the skill or file that sent it), so a source's future events can
be taken back (ovos.midi.cancel) - a skill changing its mind mid-song
without a gap - and its sounding notes stopped without touching anyone
else's.

  Engine      the instrument (synth.Synth, the built-in synth, or
              fluid.FluidSynth, with a SoundFont) a block at a time, mixed
              with clips (recorded sound at a set time, e.g. a sung voice),
              ducked while OVOS speaks or listens, to an audio output
  PortEngine  the notes to a MIDI output port: a USB keyboard, a synth, a
              drum module (no clips: they need an audio output)
"""
from __future__ import annotations

import heapq
import itertools
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

NOTE_OFF_GRACE = 0.002     # a note_off and the next note_on at the same time: off first
DUCK_SECONDS = 0.08        # how fast the music goes down and up again


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


def soft_limit(x, knee=0.8):
    """Untouched below the knee; above it bent smoothly towards 1, never past
    (music and a voice together may add up to more than the output holds)."""
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    y = x.copy()
    y[over] = np.sign(x[over]) * (knee + (1 - knee) * np.tanh((a[over] - knee) / (1 - knee)))
    return y


@dataclass
class Clip:
    at: float                 # wall time of its first sample
    samples: np.ndarray       # float, at the engine's sample rate
    gain: float = 1.0
    source: str = ""


class Engine:
    """The instrument and the clips, mixed a block at a time to an audio output."""

    def __init__(self, sink, instrument, block=1024):
        self.timeline = Timeline()
        self.sink = sink
        self.instrument = instrument
        self.name = "fluidsynth" if type(instrument).__name__ == "FluidSynth" else "builtin"
        self.block = block
        self.sample_rate = sink.sample_rate
        self.latency = sink.latency + block / sink.sample_rate
        self.clips: list[Clip] = []
        self.sounding: dict[str, set] = {}   # source -> {(ch, note)} it has on
        self.ducked = False
        self.duck_level = 0.3
        self._gain = 1.0                     # where the ducking is now
        self.t0 = None                       # wall time at which sample 0 is heard
        self.produced = 0
        self._idle = 0
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ control
    def start(self):
        if sys.platform == "emscripten":
            # a browser (Emkay): one thread, and a sleep may not really
            # sleep - so no loop; a timer makes what is due, a block at a time
            self.sink.open()
            self._tick()
        else:
            self._thread = threading.Thread(target=self._run, name="midi-engine", daemon=True)
            self._thread.start()

    def shutdown(self):
        self._stop.set()
        self.timeline.added.set()
        if self._thread:
            self._thread.join(2)
        else:
            self.sink.close()
        close = getattr(self.instrument, "close", None)
        if close:
            close()

    def add(self, *args, **kw):
        return self.timeline.add(*args, **kw)

    def add_clip(self, at, samples, sr, gain=1.0, source=""):
        """Recorded sound (float samples at `sr`) to be heard from `at` on;
        returns its length in seconds."""
        x = np.asarray(samples, dtype=np.float32)
        if sr != self.sample_rate and len(x):
            n = int(len(x) * self.sample_rate / sr)
            x = np.interp(np.arange(n) * sr / self.sample_rate, np.arange(len(x)), x).astype(np.float32)
        with self._lock:
            self.clips.append(Clip(at, x, gain, source))
        self.timeline.added.set()
        return len(x) / self.sample_rate

    def stop_source(self, source: str):
        """Its future events and clips dropped, its notes let go."""
        self.timeline.cancel(source)
        with self._lock:
            self.clips = [c for c in self.clips if c.source != source]
        now = time.time()
        for ch, note in self.sounding.pop(source, set()):
            self.timeline.add(now, "off", ch, note, source=source)
        self.timeline.add(now, "all_off", -1, source=source)   # its notes with a length, too

    def panic(self):
        self.timeline.clear()
        with self._lock:
            self.clips = []
        self.sounding.clear()
        self.timeline.add(time.time(), "all_off", -1)

    def duck(self, on: bool):
        self.ducked = on

    def status(self) -> dict:
        s = {"engine": self.name, "sample_rate": self.sample_rate, "latency": self.latency,
             "output": self.sink.name, "clips": True, "voices": self.instrument.active}
        sf = getattr(self.instrument, "soundfont", None)
        if sf:
            s["soundfont"] = sf
        return s

    # ------------------------------------------------------------ playing
    def _track(self, ev: Event):
        if ev.kind == "on" and ev.b > 0:
            self.sounding.setdefault(ev.source, set()).add((ev.ch, ev.a))
        elif ev.kind == "off" or (ev.kind == "on" and ev.b == 0):
            self.sounding.get(ev.source, set()).discard((ev.ch, ev.a))

    def _apply(self, ev: Event, offset: int):
        s = self.instrument
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

    def _busy(self) -> bool:
        return bool(self.instrument.active or self.clips or self.timeline.next_time() is not None)

    def _block(self, start, n):
        sr = self.sample_rate
        end = start + n / sr
        for ev in self.timeline.upcoming(end + 0.5):     # make notes before they are due
            if ev.kind == "on" and ev.b > 0 and ev.ch != 9 and ev.dur is not None:
                self.instrument.prepare(ev.ch, ev.a, ev.b, ev.dur)
        for ev in self.timeline.due(end):
            self._apply(ev, min(n - 1, max(0, int(round((ev.t - start) * sr)))))
        x = self.instrument.render(n)
        with self._lock:
            keep = []
            for c in self.clips:
                a = int(round((c.at - start) * sr))      # where in the block the clip begins
                s0, s1 = max(0, -a), min(len(c.samples), n - a)
                if s1 > s0:
                    x[s0 + a:s1 + a] += c.samples[s0:s1] * c.gain
                if len(c.samples) + a > n:
                    keep.append(c)
            self.clips = keep
        target = self.duck_level if self.ducked else 1.0
        if target != self._gain:
            step = n / sr / DUCK_SECONDS * (1 - self.duck_level)
            g1 = min(self._gain + step, target) if target > self._gain else max(self._gain - step, target)
            x = x * np.linspace(self._gain, g1, n)
            self._gain = g1
        elif self._gain != 1.0:
            x = x * self._gain
        x = soft_limit(x)
        self.sink.write((x * 32767).astype(np.int16), start)
        self.produced += n

    def _run(self):
        """A loop on its own thread: keeps the output fed while there is music."""
        sr, n = self.sample_rate, self.block
        self.sink.open()
        try:
            while not self._stop.is_set():
                if self.t0 is None:
                    self.timeline.added.clear()
                    if not self._busy():
                        self.timeline.added.wait(0.25)
                        continue
                    self.sink.resume()
                    self.t0 = time.time() + self.latency
                    self.produced = 0
                    self._idle = 0
                start = self.t0 + self.produced / sr
                if not self.sink.paced:     # an output that doesn't hold us back: keep time ourselves
                    ahead = start - time.time() - self.sink.latency
                    if ahead > 0:
                        time.sleep(ahead)
                self._block(start, n)
                self._idle = 0 if self._busy() else self._idle + 1
                if self._idle > 2 * sr // n:     # two quiet seconds (a reverb's tail): let the output rest
                    self.sink.pause()
                    self.t0 = None
        finally:
            self.sink.close()

    def _tick(self):
        """The same, by timer (a browser)."""
        if self._stop.is_set():
            return
        sr, n = self.sample_rate, self.block
        now = time.time()
        if self.t0 is None and self._busy():
            self.t0 = now + self.latency
            self.produced = 0
            self._idle = 0
        while self.t0 is not None:
            start = self.t0 + self.produced / sr
            if start > now + self.latency:
                break
            if start < now - 0.5:           # fell far behind (the page was busy): catch up
                self.produced += int((now - start) * sr) // n * n
                continue
            self._block(start, n)
            self._idle = 0 if self._busy() else self._idle + 1
            if self._idle > 2 * sr // n:
                self.t0 = None
        t = threading.Timer(n / sr / 2, self._tick)
        t.daemon = True
        t.start()


class PortEngine:
    """The notes to a MIDI output port (mido; python-rtmidi for the ports),
    each sent at its time from a thread."""
    name = "port"
    sample_rate = 0
    latency = 0.01

    def __init__(self, port_name):
        import mido
        self.mido = mido
        self.port = mido.open_output(port_name)
        self.port_name = port_name
        self.timeline = Timeline()
        self.sounding: dict[str, set] = {}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="midi-port", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self.timeline.added.set()
        if self._thread:
            self._thread.join(2)
        self.port.close()

    def add(self, *args, **kw):
        return self.timeline.add(*args, **kw)

    def add_clip(self, *args, **kw):
        raise RuntimeError("a MIDI port plays no recorded sound")

    def duck(self, on):
        pass                       # an instrument has its own volume knob

    _track = Engine._track

    def stop_source(self, source):
        self.timeline.cancel(source)
        now = time.time()
        for ch, note in self.sounding.pop(source, set()):
            self.timeline.add(now, "off", ch, note, source=source)

    def panic(self):
        self.timeline.clear()
        self.sounding.clear()
        self.timeline.add(time.time(), "all_off", -1)

    def status(self):
        return {"engine": self.name, "sample_rate": 0, "latency": self.latency,
                "port": self.port_name, "clips": False}

    def _run(self):
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
                    self._send(ev)
                except Exception:
                    pass

    def _send(self, ev: Event):
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
