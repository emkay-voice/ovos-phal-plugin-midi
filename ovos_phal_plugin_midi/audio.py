"""Where the built-in synth's sound goes.

  sounddevice   PortAudio, when the `sounddevice` package is installed: the
                lowest latency, and the sound card keeps the time
  paplay / pw-cat / aplay
                a player program fed raw samples - PulseAudio and PipeWire
                (paplay works on both) or plain ALSA. Nothing to install on
                a normal Linux, OVOS images included
  bus           the samples as ovos.midi.pcm messages, for a client that
                plays them itself: Emkay's page, in a browser
  null          nowhere (tests)

`auto` takes the first that works, in that order (bus in a browser).
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import time

import numpy as np

PCM_MSG = "ovos.midi.pcm"


class Sink:
    name = "null"
    paced = False          # True: write() blocks until the output takes the block
    latency = 0.15         # seconds from write() to being heard

    def __init__(self, sample_rate=24000):
        self.sample_rate = sample_rate

    def open(self):
        pass

    def write(self, pcm: np.ndarray, at: float):
        """`pcm`: int16 mono; `at`: when its first sample should be heard."""

    def pause(self):
        """Nothing to play for a while (an output may close meanwhile)."""

    def resume(self):
        pass

    def close(self):
        pass


class NullSink(Sink):
    def __init__(self, sample_rate=24000, keep=False):
        super().__init__(sample_rate)
        self.keep = keep
        self.blocks = []

    def write(self, pcm, at):
        if self.keep:
            self.blocks.append((at, pcm.copy()))


class SoundDeviceSink(Sink):
    name = "sounddevice"
    paced = True

    def __init__(self, sample_rate=24000, device=None, block=1024):
        super().__init__(sample_rate)
        import sounddevice
        self.sd = sounddevice
        self.device = device
        self.block = block
        self.stream = None
        self.latency = 0.1

    def open(self):
        self.resume()

    def resume(self):
        if self.stream is None:
            self.stream = self.sd.RawOutputStream(samplerate=self.sample_rate, channels=1, dtype="int16",
                                                  blocksize=self.block, device=self.device, latency="low")
            self.stream.start()
            self.latency = float(self.stream.latency) + self.block / self.sample_rate

    def write(self, pcm, at):
        self.stream.write(pcm.tobytes())

    def pause(self):
        self.close()

    def close(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


PLAYERS = {   # raw s16le mono from stdin
    "paplay": lambda sr: ["paplay", "--raw", "--format=s16le", f"--rate={sr}", "--channels=1",
                          "--latency-msec=60", "--client-name=ovos-midi"],
    "pw-cat": lambda sr: ["pw-cat", "--playback", "--raw", "--format=s16", f"--rate={sr}", "--channels=1",
                          "--latency=60ms", "-"],
    "aplay": lambda sr: ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(sr), "-c", "1",
                         "--buffer-time=100000", "-"],
}


class PipeSink(Sink):
    """A player program, fed in real time (we keep the time, so the pipe
    never fills up and adds delay)."""

    def __init__(self, sample_rate=24000, player="paplay", latency=0.15):
        super().__init__(sample_rate)
        self.name = player
        self.cmd = PLAYERS[player](sample_rate)
        self.latency = latency
        self.proc = None

    def open(self):
        pass

    def resume(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            # a little silence first: the player starts with a full buffer
            self.proc.stdin.write(bytes(int(self.sample_rate * 0.05) * 2))

    def write(self, pcm, at):
        if self.proc is None:
            self.resume()
        try:
            self.proc.stdin.write(pcm.tobytes())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.proc = None

    def pause(self):
        self.close()

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(1)
            except Exception:
                self.proc.kill()
            self.proc = None


class BusSink(Sink):
    """ovos.midi.pcm: {"seq", "sample_rate", "at" (wall time of the first
    sample), "pcm" (base64 s16le mono)}, every few blocks."""
    name = "bus"

    def __init__(self, bus, sample_rate=24000, latency=0.4, chunk_seconds=0.17):
        super().__init__(sample_rate)
        self.bus = bus
        self.latency = latency
        self.chunk = int(chunk_seconds * sample_rate)
        self.buf, self.buf_at, self.seq = [], None, 0

    def write(self, pcm, at):
        from ovos_bus_client.message import Message
        if self.buf_at is None:
            self.buf_at = at
        self.buf.append(pcm)
        if sum(len(b) for b in self.buf) >= self.chunk:
            data = np.concatenate(self.buf)
            self.bus.emit(Message(PCM_MSG, {"seq": self.seq, "sample_rate": self.sample_rate,
                                            "at": self.buf_at,
                                            "pcm": base64.b64encode(data.tobytes()).decode()}))
            self.seq += 1
            self.buf, self.buf_at = [], None

    def pause(self):
        self.buf, self.buf_at = [], None


def make_sink(kind="auto", sample_rate=24000, bus=None, device=None):
    """The output named by `kind`, or for "auto" the first that works here."""
    if kind == "null":
        return NullSink(sample_rate)
    if kind == "bus" or (kind == "auto" and sys.platform == "emscripten"):
        return BusSink(bus, sample_rate)
    if kind in ("auto", "sounddevice"):
        try:
            return SoundDeviceSink(sample_rate, device)
        except Exception:
            if kind == "sounddevice":
                raise
    for player in PLAYERS:
        if kind in ("auto", player) and shutil.which(player):
            return PipeSink(sample_rate, player)
    if kind != "auto":
        raise RuntimeError(f"audio output {kind!r} is not available")
    return None
