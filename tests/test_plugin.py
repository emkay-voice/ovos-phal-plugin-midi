import base64
import io
import time

import mido
from ovos_bus_client.message import Message

from ovos_phal_plugin_midi import MidiPlugin
from ovos_phal_plugin_midi.audio import NullSink


def plugin(bus):
    p = MidiPlugin(bus=bus, config={"audio": "null", "synth": "builtin", "inputs": []})
    assert p.engine.name == "builtin" and isinstance(p.engine.sink, NullSink)
    return p


def small_file(seconds=0.6):
    mid = mido.MidiFile()
    tr = mido.MidiTrack()
    mid.tracks.append(tr)
    tr.append(mido.Message("program_change", program=0, time=0))
    tr.append(mido.Message("note_on", note=60, velocity=90, time=0))
    tr.append(mido.Message("note_off", note=60, time=mid.ticks_per_beat * int(seconds * 2)))
    b = io.BytesIO()
    mid.save(file=b)
    return b.getvalue()


def test_status_answers(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.status"))
    r = bus.last("ovos.midi.status.response")
    assert r.data["engine"] == "builtin" and r.data["latency"] > 0 and "ports" in r.data
    p.shutdown()


def test_events_are_scheduled_at_their_time(bus):
    p = plugin(bus)
    at = time.time() + 5
    bus.emit(Message("ovos.midi.events", {"source": "jam", "at": at, "events": [
        ["program", 0, 0, 33], ["note", 0.5, 0, 40, 100, 0.4], ["on", 1, 9, 36, 110],
        ["bogus", 1], ["note", "x"]]}))
    evs = p.engine.timeline.upcoming(at + 10)
    assert [(e.kind, round(e.t - at, 3)) for e in evs] == [("program", 0), ("on", 0.5), ("on", 1)]
    assert {e.source for e in evs} == {"jam"}
    bus.emit(Message("ovos.midi.cancel", {"source": "jam", "after": at + 0.25}))
    assert bus.last("ovos.midi.cancel.response").data["cancelled"] == 2
    p.shutdown()


def test_play_a_file_and_hear_the_end(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.play", {"data": base64.b64encode(small_file()).decode(), "source": "t"}))
    started = bus.last("ovos.midi.play.started")
    assert started and 0.4 < started.data["duration"] < 1
    for _ in range(60):
        if "ovos.midi.play.ended" in bus.types():
            break
        time.sleep(0.1)
    assert "ovos.midi.play.ended" in bus.types()
    p.shutdown()


def test_stop_drops_a_looping_file(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.play", {"data": base64.b64encode(small_file()).decode(),
                                        "source": "t", "loop": True}))
    assert "t" in p.players
    bus.emit(Message("mycroft.stop"))
    assert not p.players
    p.shutdown()


def test_bad_file_says_so(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.play", {"path": "/nonexistent.mid", "source": "t"}))
    assert bus.last("ovos.midi.play.failed").data["source"] == "t"
    p.shutdown()


def wav_bytes(seconds=0.3, sr=16000):
    import wave
    import numpy as np
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.sin(np.arange(int(seconds * sr)) / 5) * 8000).astype("<i2").tobytes())
    return b.getvalue()


def test_clip_comes_back_with_its_time(bus):
    p = plugin(bus)
    at = time.time() + 3
    bus.emit(Message("ovos.midi.clip", {"data": base64.b64encode(wav_bytes()).decode(),
                                        "source": "karaoke", "at": at}))
    r = bus.last("ovos.midi.clip.response").data
    assert r["at"] == at and abs(r["duration"] - 0.3) < 0.01 and len(p.engine.clips) == 1
    p.shutdown()


def test_play_at_a_given_time(bus):
    p = plugin(bus)
    at = time.time() + 4
    bus.emit(Message("ovos.midi.play", {"data": base64.b64encode(small_file()).decode(),
                                        "source": "k", "at": at}))
    assert bus.last("ovos.midi.play.started").data["at"] == at
    p.shutdown()


def test_ducks_while_ovos_speaks(bus):
    p = plugin(bus)
    bus.emit(Message("recognizer_loop:audio_output_start"))
    assert p.engine.ducked
    bus.emit(Message("ovos.audio.output.ended"))
    assert not p.engine.ducked
    p.shutdown()


def test_the_status_carries_the_settings(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.status"))
    s = bus.last("ovos.midi.status.response").data["settings"]
    assert s["synth"] == "builtin" and s["inputs"] == [] and s["thru"] is False and s["duck"] is True
    p.shutdown()


def test_new_settings_take_effect_at_once(bus):
    p = plugin(bus)
    first = p.engine
    bus.emit(Message("ovos.midi.reload", {"config": {"audio": "null", "synth": "builtin", "inputs": [],
                                                     "thru": True, "duck": False}}))
    r = bus.last("ovos.midi.reload.response").data
    assert r["engine"] == "builtin" and r["settings"]["thru"] is True and r["settings"]["duck"] is False
    assert p.engine is not first, "a new synth"
    assert p.handle_duck not in bus.handlers.get("recognizer_loop:audio_output_start", []), "no ducking"
    p.shutdown()


def test_a_sound_output_that_is_not_there_is_said_not_fatal(bus):
    p = plugin(bus)
    bus.emit(Message("ovos.midi.reload", {"config": {"audio": "no-such-player", "synth": "builtin", "inputs": []}}))
    r = bus.last("ovos.midi.reload.response").data
    assert r["engine"] == "none" and "no-such-player" in r["error"]
    p.shutdown()
