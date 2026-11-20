"""MIDI ports: instruments plugged into the device (USB, or ALSA's sequencer).

Listing and opening them needs python-rtmidi (mido's port backend). Without
it the plugin simply has no ports; its synth still plays.

Inputs: every note, pedal and knob from a keyboard goes on the bus as
ovos.midi.input, so a skill can follow what you play; with `thru` the
device also plays it on its own synth. Keyboards plugged in later are
found (the list is looked at again every few seconds).
"""
from __future__ import annotations

import fnmatch
import threading

try:
    import mido
    mido.get_input_names()           # raises when there is no port backend
    HAVE_PORTS = True
except Exception:                    # no python-rtmidi
    HAVE_PORTS = False

RESCAN = 5.0
#: ALSA's own ports and the plugin's own: never listened to (a loop)
IGNORE = ("Midi Through*", "*RtMidi*", "*ovos-midi*")


def names() -> dict:
    if not HAVE_PORTS:
        return {"inputs": [], "outputs": [], "available": False}
    try:
        return {"inputs": mido.get_input_names(), "outputs": mido.get_output_names(), "available": True}
    except Exception:
        return {"inputs": [], "outputs": [], "available": False}


def find_output(pattern: str) -> str | None:
    """The first output port whose name matches `pattern` (a glob, or part of the name)."""
    for name in names()["outputs"]:
        if fnmatch.fnmatch(name, pattern) or pattern.lower() in name.lower():
            return name
    return None


def message_data(port: str, msg) -> dict:
    d = {"port": port, "type": msg.type}
    for k in ("channel", "note", "velocity", "control", "value", "program", "pitch"):
        if hasattr(msg, k):
            d[k] = getattr(msg, k)
    return d


class Inputs:
    """Listens to the matching input ports; `on_message(port, msg)` for each."""

    def __init__(self, patterns, on_message):
        self.patterns = list(patterns or [])
        self.on_message = on_message
        self.open: dict[str, object] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="midi-inputs", daemon=True)

    def start(self):
        if HAVE_PORTS and self.patterns:
            self._thread.start()
        return self

    def _wanted(self, name):
        if any(fnmatch.fnmatch(name, p) for p in IGNORE):
            return False
        return any(fnmatch.fnmatch(name, p) or p.lower() in name.lower() for p in self.patterns)

    def _run(self):
        while not self._stop.is_set():
            try:
                present = set(mido.get_input_names())
            except Exception:
                present = set()
            for name in list(self.open):
                if name not in present:           # unplugged
                    try:
                        self.open.pop(name).close()
                    except Exception:
                        pass
            for name in present:
                if name not in self.open and self._wanted(name):
                    try:
                        self.open[name] = mido.open_input(
                            name, callback=lambda m, n=name: self.on_message(n, m))
                    except Exception:
                        pass
            self._stop.wait(RESCAN)

    def stop(self):
        self._stop.set()
        for p in self.open.values():
            try:
                p.close()
            except Exception:
                pass
        self.open.clear()
