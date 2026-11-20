"""Plays a few timed notes through the real audio output (paplay/pw-cat/aplay)
while parec records what the sound server plays; prints when each note was
heard against when it was scheduled."""
import subprocess
import sys
import time
import wave

import numpy as np

from ovos_phal_plugin_midi.audio import make_sink
from ovos_phal_plugin_midi.engine import BuiltinEngine

kind = sys.argv[1] if len(sys.argv) > 1 else "auto"
sink = make_sink(kind)
print("output:", sink.name, "latency", sink.latency)
sink_name = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
rec = subprocess.Popen(["parec", "-d", f"{sink_name}.monitor", "--format=s16le", "--rate=24000",
                        "--channels=1", "--latency-msec=10"], stdout=subprocess.PIPE)
t_rec = time.time()
e = BuiltinEngine(sink)
e.start()
t = time.time() + 1.0
times = [t + i * 0.5 for i in range(6)]
for at in times:
    e.add(at, "on", 9, 37, 120, dur=0.05)      # side stick: a sharp click
time.sleep(5)
e.shutdown()
rec.terminate()
x = np.frombuffer(rec.stdout.read(), dtype=np.int16).astype(float) / 32768
with wave.open("/tmp/try_sound.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
    w.writeframes((x * 32767).astype(np.int16).tobytes())
env = np.abs(x)
onsets, i = [], 0
while i < len(env):
    if env[i] > 0.05:
        onsets.append(t_rec + i / 24000)
        i += 2400
    i += 1
print("heard", len(onsets), "of", len(times))
d = [o - s for o, s in zip(onsets, times)]
print("offset vs schedule (ms):", [round(v * 1000) for v in d])
print("spacing (ms):", [round((b - a) * 1000) for a, b in zip(onsets, onsets[1:])])
