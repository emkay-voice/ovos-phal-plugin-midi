"""Playing a MIDI file (.mid, .kar) through an engine.

The file is read with mido into events at their times; a thread hands them
to the engine a couple of seconds ahead, so a long file never floods the
timeline and stop is immediate. Tempo can be scaled and the music
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
        self._stop = threading.Event()
        self.started = None
        self._thread = threading.Thread(target=self._run, name=f"midi-file-{source}", daemon=True)

    def start(self):
        self.started = time.time() + self.engine.latency + 0.1
        self._thread.start()
        return self

    @property
    def duration(self) -> float:
        return self.length / self.tempo

    def stop(self):
        self._stop.set()
        self.engine.stop_source(self.source)

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

    def _run(self):
        start = self.started
        while True:
            for t, msg in self.events:
                at = start + t / self.tempo
                wait = at - AHEAD - time.time()
                if wait > 0 and self._stop.wait(wait):
                    return
                if self._stop.is_set():
                    return
                self._schedule(at, msg)
            if not self.loop:
                break
            start += self.length / self.tempo
        if not self._stop.wait(max(0.0, start + self.length / self.tempo - time.time() + 1.0)):
            if self.on_end:
                self.on_end(self)
