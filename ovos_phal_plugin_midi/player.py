"""Playing a MIDI file (.mid, .kar) through an engine.

The file is read with mido into events at their times; a timer hands them
to the engine a couple of seconds ahead, so a long file never floods the
timeline and stop is immediate. (A timer, not a thread with a loop: the
same works in a browser, where there is one thread.) Tempo can be scaled and the music
transposed (not the drums); channels can be muted (a karaoke skill mutes
the melody while its own voice sings it).
"""
from __future__ import annotations

import io
import threading
import time

import mido

AHEAD = 2.0          # seconds of the file handed to the engine in advance


def events(data: bytes):
    """[(seconds, mido.Message)] of the file, in order."""
    mid = mido.MidiFile(file=io.BytesIO(data))
    out, t = [], 0.0
    for msg in mid:                 # mido merges the tracks, time in seconds
        t += msg.time
        if not msg.is_meta and msg.type in ("note_on", "note_off", "program_change", "control_change"):
            out.append((t, msg))
    return out, t


class FilePlayer:
    def __init__(self, engine, data: bytes, source: str, loop=False, transpose=0, tempo=1.0,
                 mute=(), on_end=None):
        self.engine = engine
        self.events, self.length = events(data)
        self.source = source
        self.loop = loop
        self.transpose = transpose
        self.tempo = tempo or 1.0
        self.mute = set(mute)
        self.on_end = on_end
        self.stopped = False
        self.started = None
        self._next = 0          # index of the next event to hand over
        self._round = 0.0       # start of the current time through the file
        self._timer = None
        self._lock = threading.Lock()

    def start(self):
        self.started = time.time() + self.engine.latency + 0.1
        self._round = self.started
        self._feed()
        return self

    @property
    def duration(self) -> float:
        return self.length / self.tempo

    def stop(self):
        with self._lock:
            self.stopped = True
            if self._timer:
                self._timer.cancel()
        self.engine.stop_source(self.source)

    def _later(self, seconds, fn):
        self._timer = threading.Timer(max(0.0, seconds), fn)
        self._timer.daemon = True
        self._timer.start()

    def _schedule(self, t0, msg):
        e, ch = self.engine, msg.channel
        if ch in self.mute and msg.type in ("note_on", "note_off"):
            return
        if msg.type == "note_on" and msg.velocity > 0:
            note = msg.note + (self.transpose if ch != 9 else 0)
            if 0 <= note < 128:
                e.add(t0, "on", ch, note, msg.velocity, source=self.source)
        elif msg.type in ("note_on", "note_off"):
            note = msg.note + (self.transpose if ch != 9 else 0)
            if 0 <= note < 128:
                e.add(t0, "off", ch, note, source=self.source)
        elif msg.type == "program_change":
            e.add(t0, "program", ch, msg.program, source=self.source)
        elif msg.type == "control_change":
            e.add(t0, "cc", ch, msg.control, msg.value, source=self.source)

    def _feed(self):
        """Hand over what is due in the next AHEAD seconds, then come back."""
        with self._lock:
            if self.stopped:
                return
            until = time.time() + AHEAD
            while True:
                if self._next >= len(self.events):
                    end = self._round + self.length / self.tempo
                    if not self.loop:
                        self._later(end - time.time() + 0.5, self._ended)
                        return
                    self._round, self._next = end, 0
                    if not self.events:
                        return
                t, msg = self.events[self._next]
                at = self._round + t / self.tempo
                if at > until:
                    break
                self._schedule(at, msg)
                self._next += 1
            self._later(AHEAD / 2, self._feed)

    def _ended(self):
        with self._lock:
            if self.stopped:
                return
            self.stopped = True
        if self.on_end:
            self.on_end(self)
