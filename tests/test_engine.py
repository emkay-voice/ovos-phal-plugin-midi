import time

import numpy as np

from ovos_phal_plugin_midi.audio import NullSink
from ovos_phal_plugin_midi.engine import Engine, Timeline
from ovos_phal_plugin_midi.synth import Synth

SR = 24000


def run(events, seconds=1.5):
    sink = NullSink(SR, keep=True)
    sink.latency = 0.05
    e = Engine(sink, Synth(SR), block=512)
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


def test_browser_mode_plays_on_time_with_a_timer(monkeypatch):
    """In a browser (Emkay) there is no loop thread: a timer makes the blocks."""
    from ovos_phal_plugin_midi import engine as eng
    monkeypatch.setattr(eng.sys, "platform", "emscripten")
    sink = NullSink(SR, keep=True)
    sink.latency = 0.3
    e = Engine(sink, Synth(SR), block=1024)
    e.start()
    assert e._thread is None
    t = time.time() + 0.5
    e.add(t, "on", 9, 36, 120, dur=0.1)
    time.sleep(1.2)
    e.shutdown()
    x = np.concatenate([b for _, b in sink.blocks]).astype(float) / 32767
    onset = int(np.argmax(np.abs(x) > 0.01))
    assert abs((sink.blocks[0][0] + onset / SR) - t) < 0.002
    starts = [a for a, _ in sink.blocks]
    assert np.allclose(np.diff(starts), 1024 / SR)           # back to back, no gaps


def test_a_clip_is_heard_at_its_time():
    clip = np.zeros(SR // 2, dtype=np.float32)
    clip[0] = 0.9                                    # one click, at its first sample
    x, t0, t, _ = run([lambda e, t: e.add_clip(t + 0.3, clip, SR, source="voice")])
    onset = int(np.argmax(np.abs(x) > 0.5))
    assert abs((t0 + onset / SR) - (t + 0.3)) < 1.5 / SR


def test_a_clip_is_resampled_and_stopped_with_its_source():
    sink = NullSink(SR, keep=True)
    e = Engine(sink, Synth(SR), block=512)
    dur = e.add_clip(time.time() + 10, np.ones(44100), 44100, source="voice")
    assert abs(dur - 1.0) < 0.001 and len(e.clips[0].samples) == SR
    e.stop_source("voice")
    assert not e.clips


def test_ducking_lowers_and_restores():
    sink = NullSink(SR, keep=True)
    e = Engine(sink, Synth(SR, reverb=False), block=512)
    e.add_clip(0, np.full(SR * 2, 0.5, dtype=np.float32), SR)
    e.t0 = 0
    for _ in range(10):
        e._block(e.produced / SR, 512)
    e.duck(True)
    for _ in range(10):
        e._block(e.produced / SR, 512)
    low = sink.blocks[-1][1].astype(float).mean() / 32767
    e.duck(False)
    for _ in range(10):
        e._block(e.produced / SR, 512)
    high = sink.blocks[-1][1].astype(float).mean() / 32767
    assert abs(low - 0.15) < 0.01 and abs(high - 0.5) < 0.01


def test_fluidsynth_plays_notes_on_their_sample():
    import pytest
    from ovos_phal_plugin_midi import find_soundfont
    sf = find_soundfont()
    try:
        from ovos_phal_plugin_midi.fluid import FluidSynth
        fs = FluidSynth(sf, SR, reverb=False) if sf else None
    except Exception:
        fs = None
    if fs is None:
        pytest.skip("no FluidSynth or SoundFont here")
    fs.note_on(9, 37, 120, offset=300, dur=0.05)
    x = fs.render(1024)
    assert np.abs(x[:290]).max() < 1e-4 and np.abs(x[300:600]).max() > 0.01
    fs.program_change(0, 40)
    fs.note_on(0, 69, 100, dur=0.1)
    assert np.abs(np.concatenate([fs.render(1024) for _ in range(5)])).max() > 0.01
    assert fs.active == 0
    fs.close()
