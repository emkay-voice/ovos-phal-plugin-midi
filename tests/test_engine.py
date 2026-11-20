import time

import numpy as np

from ovos_phal_plugin_midi.audio import NullSink
from ovos_phal_plugin_midi.engine import BuiltinEngine, Timeline
from ovos_phal_plugin_midi.synth import Synth

SR = 24000


def run(events, seconds=1.5):
    sink = NullSink(SR, keep=True)
    sink.latency = 0.05
    e = BuiltinEngine(sink, block=512)
    e.start()
    t = time.time() + 0.2
    for ev in events:
        ev(e, t)
    time.sleep(seconds)
    e.shutdown()
    x = np.concatenate([b for _, b in sink.blocks]).astype(float) / 32767
    return x, sink.blocks[0][0], t, e


def test_note_lands_on_its_sample():
    x, t0, t, _ = run([lambda e, t: e.add(t + 0.25, "on", 9, 36, 120, dur=0.1)])
    onset = int(np.argmax(np.abs(x) > 0.01))
    assert abs((t0 + onset / SR) - (t + 0.25)) < 0.002


def test_held_note_released_at_note_off():
    def evs(e, t):
        e.add(t - 0.01, "program", 0, 19)            # an organ: it holds
        e.add(t, "on", 0, 60, 100)
        e.add(t + 0.4, "off", 0, 60)
    x, t0, t, _ = run([evs], 1.6)
    k = lambda s: int((t + s - t0) * SR)
    loud = np.abs(x[k(0.1):k(0.35)]).max()
    after = np.abs(x[k(0.7):k(0.8)]).max()
    assert loud > 0.05 and after < loud * 0.25      # only the room's tail is left


def test_cancel_takes_back_one_sources_future():
    def evs(e, t):
        e.add(t + 0.1, "on", 9, 36, 120, dur=0.1, source="jam")
        e.add(t + 0.8, "on", 9, 36, 120, dur=0.1, source="jam")
        e.add(t + 0.8, "on", 9, 38, 120, dur=0.1, source="other")
        assert e.timeline.cancel("jam", after=t + 0.5) == 1
    _, _, _, e = run([evs], 1.3)
    assert e.timeline.next_time() is None


def test_timeline_order_off_before_on():
    tl = Timeline()
    tl.add(1.0, "on", 0, 60, 90)
    tl.add(1.0, "off", 0, 60)
    assert [e.kind for e in tl.due(2.0)] == ["off", "on"]


def test_synth_pedal_holds_notes():
    s = Synth(SR, reverb=False)
    s.program_change(0, 19)
    s.note_on(0, 60, 100)
    s.control_change(0, 64, 127)
    s.note_off(0, 60)
    s.render(SR // 2)
    assert s.voices[0].off_at is None
    s.control_change(0, 64, 0)
    assert s.voices[0].off_at is not None


def test_every_program_and_drum_sounds():
    s = Synth(SR, reverb=False)
    for prog in range(0, 128, 3):
        s.program_change(0, prog)
        s.note_on(0, 60, 100, dur=0.2)
        assert np.abs(s.render(2400)).max() > 0.01, prog
        s.all_off(hard=True)
    for key in (35, 36, 38, 42, 46, 49, 51, 54, 56):
        s.note_on(9, key, 100)
        assert np.abs(s.render(2400)).max() > 0.01, key
        s.all_off(hard=True)
