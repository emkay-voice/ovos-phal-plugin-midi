"""ovos-phal-plugin-midi: a MIDI instrument for OpenVoiceOS.

Gives the device a General MIDI synthesizer on its message bus, so a skill
can play music note by note, timed to the millisecond and changed while it
plays, or hand it a MIDI file. It plays on:

  * a real MIDI instrument plugged into the device (USB keyboard, synth,
    drum module), when `output_port` names one;
  * FluidSynth with a SoundFont, when pyfluidsynth and a SoundFont are there;
  * otherwise its own synth in numpy (nothing to install), to the sound card,
    or over the bus to a client that plays it (Emkay in a browser).

Keyboards plugged into the device come the other way: what you play goes on
the bus (ovos.midi.input), and with `thru` it sounds on the device.

The bus messages are in the README. The control panel's page (panel/) shows
the status, sets what is below and plays a test; new settings take effect at
once with ovos.midi.reload {"config": the plugin's section}.

Configuration, in mycroft.conf:

    "PHAL": {
      "ovos-phal-plugin-midi": {
        "synth": "auto",          # auto | builtin | fluidsynth | port
        "audio": "auto",          # auto | sounddevice | paplay | pw-cat | aplay | bus
        "soundfont": "",          # a .sf2 for FluidSynth (else a usual place is looked in)
        "output_port": "",        # a MIDI output port (name or part of it)
        "inputs": ["*"],          # input ports to listen to ([] for none)
        "thru": false             # play what comes in on the device's own synth
      }
    }
"""
from __future__ import annotations

import base64
import io
import os
import sys
import threading
import time
from pathlib import Path

from ovos_bus_client.message import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.log import LOG

from . import ports
from .audio import make_sink
from .engine import Engine, PortEngine
from .player import FilePlayer
from .synth import Synth

PLUGIN = "ovos-phal-plugin-midi"
SOUNDFONTS = [  # where distributions put a General MIDI SoundFont
    "/usr/share/sounds/sf2/FluidR3_GM.sf2", "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/default-GM.sf2", "/usr/share/soundfonts/default.sf2",
    "/usr/share/sounds/sf2/TimGM6mb.sf2", "/usr/share/soundfonts/GeneralUser-GS.sf2",
]
KINDS = {"note", "on", "off", "program", "cc", "all_off"}
#: the settings a control panel can change, with their defaults
SETTINGS = {"synth": "auto", "audio": "auto", "soundfont": "", "output_port": "", "inputs": ["*"],
            "thru": False, "duck": True, "duck_volume": 0.3, "gain": 0.45, "reverb": True}


def have_fluidsynth() -> bool:
    try:
        import fluidsynth  # noqa: F401 - pyfluidsynth
        return True
    except Exception:  # ImportError, or the library missing under it
        return False


def wav_samples(data: bytes):
    """(mono float samples, sample rate) of a WAV file."""
    import wave
    import numpy as np
    with wave.open(io.BytesIO(data)) as w:
        ch, width, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    elif width == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2 ** 31
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    else:
        raise ValueError(f"{8 * width}-bit WAV")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def find_soundfont(configured: str = "") -> str | None:
    for p in [configured] + SOUNDFONTS:
        if p and os.path.isfile(os.path.expanduser(p)):
            return os.path.expanduser(p)
    return None


def make_engine(cfg: dict, bus=None):
    """The engine the configuration asks for, or the best one available."""
    want = cfg.get("synth", "auto")
    if want in ("auto", "port") and cfg.get("output_port"):
        name = ports.find_output(cfg["output_port"])
        if name:
            try:
                return PortEngine(name)
            except Exception as e:
                LOG.warning(f"{PLUGIN}: MIDI port {name!r}: {e}")
        elif want == "port":
            LOG.warning(f"{PLUGIN}: no MIDI output port like {cfg['output_port']!r}")
    sr = int(cfg.get("sample_rate", 24000))
    sink = make_sink(cfg.get("audio", "auto"), sr, bus, cfg.get("audio_device"))
    if sink is None:
        LOG.warning(f"{PLUGIN}: no way to play sound found (install sounddevice, or pulseaudio-utils / alsa-utils)")
        return None
    reverb = bool(cfg.get("reverb", True))
    instrument = None
    if want in ("auto", "fluidsynth"):
        sf = find_soundfont(cfg.get("soundfont", ""))
        if sf:
            try:
                from .fluid import FluidSynth
                instrument = FluidSynth(sf, sink.sample_rate, gain=float(cfg.get("fluidsynth_gain", 0.8)),
                                        reverb=reverb)
            except Exception as e:
                (LOG.warning if want == "fluidsynth" else LOG.info)(f"{PLUGIN}: FluidSynth: {e}")
    if instrument is None:
        instrument = Synth(sink.sample_rate, gain=float(cfg.get("gain", 0.45)), reverb=reverb)
    engine = Engine(sink, instrument)
    engine.duck_level = float(cfg.get("duck_volume", 0.3))
    return engine


#: what OVOS sends when it starts and stops speaking or listening: the music
#: goes down meanwhile (as ovos-audio's AudioService ducks its own)
SPEAKING = {"recognizer_loop:audio_output_start": True, "ovos.audio.output.started": True,
            "recognizer_loop:audio_output_end": False, "ovos.audio.output.ended": False}
LISTENING = {"recognizer_loop:record_begin": True, "ovos.listener.record.started": True,
             "recognizer_loop:wakeword": True, "ovos.listener.wakeword": True,
             "recognizer_loop:record_end": False, "ovos.listener.record.ended": False}
LISTEN_AT_MOST = 15.0      # seconds: a listening that never said it ended


class MidiPlugin(PHALPlugin):

    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name=PLUGIN, config=config or {})
        self.engine = self._make_engine()
        self.players: dict[str, FilePlayer] = {}
        self._lock = threading.Lock()
        on = self.bus.on
        on("ovos.midi.status", self.handle_status)
        on("ovos.midi.ports", self.handle_ports)
        on("ovos.midi.events", self.handle_events)
        on("ovos.midi.cancel", self.handle_cancel)
        on("ovos.midi.play", self.handle_play)
        on("ovos.midi.stop", self.handle_stop)
        on("ovos.midi.panic", self.handle_panic)
        on("ovos.midi.clip", self.handle_clip)
        on("mycroft.stop", self.handle_global_stop)
        on("ovos.midi.reload", self.handle_reload)
        self._speaking = False
        self._listening = 0.0      # since when (0: not listening)
        if self.config.get("duck", True):
            for t in list(SPEAKING) + list(LISTENING):
                on(t, self.handle_duck)
        self.inputs = ports.Inputs(self.config.get("inputs", ["*"]), self._on_input).start()

    def _make_engine(self):
        self.engine_error = ""
        try:
            engine = make_engine(self.config, self.bus)
        except Exception as e:  # noqa: BLE001 - e.g. an audio output that isn't here
            LOG.error(f"{PLUGIN}: {e}")
            self.engine_error = str(e)
            return None
        if engine:
            engine.start()
            LOG.info(f"{PLUGIN}: {engine.status()}")
        return engine

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _source(message) -> str:
        return str(message.data.get("source") or message.context.get("skill_id") or "bus")

    def _reply(self, message, data):
        self.bus.emit(message.response(data))

    def status(self) -> dict:
        s = self.engine.status() if self.engine else {"engine": "none", "latency": 0.0, "sample_rate": 0}
        return {**s, "time": time.time(), "playing": sorted(self.players),
                "latency_ms": round(float(s.get("latency") or 0) * 1000),
                "ports": ports.names(), "listening": sorted(self.inputs.open),
                "settings": {k: self.config.get(k, d) for k, d in SETTINGS.items()},
                "found_soundfont": find_soundfont(self.config.get("soundfont", "")),
                "have_fluidsynth": have_fluidsynth(), "error": self.engine_error}

    # ------------------------------------------------------------ handlers
    def handle_status(self, message):
        self._reply(message, self.status())

    def handle_ports(self, message):
        self._reply(message, ports.names())

    def handle_events(self, message):
        """{"source", "at" (wall time; default now + latency), "events": [[kind, t, ...]]}

        kinds: ["note", t, ch, note, vel, dur]   a note with its length
               ["on", t, ch, note, vel] / ["off", t, ch, note]
               ["program", t, ch, program] / ["cc", t, ch, control, value]
               ["all_off", t, ch]  (ch -1: every channel)
        `t` is seconds after `at`."""
        if not self.engine:
            return
        d = message.data
        src = self._source(message)
        at = float(d.get("at") or time.time() + self.engine.latency)
        add = self.engine.add
        for ev in d.get("events") or []:
            try:
                kind, t = ev[0], at + float(ev[1])
                if kind not in KINDS:
                    continue
                ch = int(ev[2]) if len(ev) > 2 else -1
                if kind == "note":
                    add(t, "on", ch & 15, int(ev[3]) & 127, max(1, min(127, int(ev[4]))),
                        dur=max(0.01, float(ev[5])), source=src)
                elif kind == "on":
                    add(t, "on", ch & 15, int(ev[3]) & 127, max(0, min(127, int(ev[4]))), source=src)
                elif kind == "off":
                    add(t, "off", ch & 15, int(ev[3]) & 127, source=src)
                elif kind == "program":
                    add(t, "program", ch & 15, int(ev[3]) & 127, source=src)
                elif kind == "cc":
                    add(t, "cc", ch & 15, int(ev[3]) & 127, int(ev[4]) & 127, source=src)
                else:
                    add(t, "all_off", ch, source=src)
            except (IndexError, TypeError, ValueError):
                LOG.debug(f"{PLUGIN}: bad event {ev!r}")

    def handle_cancel(self, message):
        """{"source", "after" (wall time; default now)}: its events from then
        on are dropped - to send them again, changed."""
        if self.engine:
            n = self.engine.timeline.cancel(self._source(message),
                                            float(message.data.get("after") or time.time()))
            self._reply(message, {"cancelled": n})

    def _file_bytes(self, d) -> bytes:
        if d.get("data"):
            return base64.b64decode(d["data"])
        uri = d.get("uri") or d.get("path") or ""
        if uri.startswith("file://"):
            from urllib.parse import unquote
            uri = unquote(uri[len("file://"):])
        if uri.startswith(("http://", "https://")):
            import urllib.request
            with urllib.request.urlopen(uri, timeout=20) as r:
                return r.read()
        return Path(uri).expanduser().read_bytes()

    def handle_play(self, message):
        """{"uri" | "path" | "data" (base64), "source", "at" (wall time; default
        now + latency), "loop", "transpose", "tempo", "mute": [channels]}
        -> ovos.midi.play.started {at, duration} / .ended"""
        d = message.data
        src = self._source(message)
        if not self.engine:
            self.bus.emit(message.reply("ovos.midi.play.failed", {"source": src, "error": "no synth"}))
            return
        try:
            data = self._file_bytes(d)
            self._stop_player(src)
            p = FilePlayer(self.engine, data, src, loop=bool(d.get("loop")),
                           transpose=int(d.get("transpose", 0)), tempo=float(d.get("tempo", 1.0)),
                           mute=[int(c) for c in d.get("mute", [])], on_end=self._ended)
        except Exception as e:
            LOG.warning(f"{PLUGIN}: cannot play {d.get('uri') or d.get('path')}: {e}")
            self.bus.emit(message.reply("ovos.midi.play.failed", {"source": src, "error": str(e)}))
            return
        with self._lock:
            self.players[src] = p
        p.start(d.get("at"))
        self.bus.emit(message.reply("ovos.midi.play.started",
                                    {"source": src, "at": p.started, "duration": p.duration, "loop": p.loop}))

    def handle_clip(self, message):
        """{"path" | "uri" | "data" (base64 WAV), "source", "at" (wall time;
        default now + latency), "gain"}: recorded sound, heard from `at` on,
        in the same output as the music and in time with it (a voice that
        sings to it). -> ovos.midi.clip.response {at, duration} or {error}"""
        d = message.data
        src = self._source(message)
        try:
            if not self.engine:
                raise RuntimeError("no synth")
            samples, sr = wav_samples(self._file_bytes(d))
            at = float(d.get("at") or time.time() + self.engine.latency)
            dur = self.engine.add_clip(at, samples, sr, float(d.get("gain", 1.0)), src)
            self._reply(message, {"source": src, "at": at, "duration": dur})
        except Exception as e:
            LOG.warning(f"{PLUGIN}: clip: {e}")
            self._reply(message, {"source": src, "error": str(e)})

    def handle_duck(self, message):
        t = message.msg_type
        if t in SPEAKING:
            self._speaking = SPEAKING[t]
        else:
            self._listening = time.time() if LISTENING[t] else 0.0
        listening = self._listening and time.time() - self._listening < LISTEN_AT_MOST
        if self.engine:
            self.engine.duck(bool(self._speaking or listening))

    def _ended(self, player):
        with self._lock:
            if self.players.get(player.source) is player:
                del self.players[player.source]
        self.bus.emit(Message("ovos.midi.play.ended", {"source": player.source}))

    def _stop_player(self, src):
        with self._lock:
            p = self.players.pop(src, None)
        if p:
            p.stop()

    def handle_stop(self, message):
        """{"source"}: that source's file and notes; no source: everything."""
        src = message.data.get("source")
        if src:
            self._stop_player(src)
            if self.engine:
                self.engine.stop_source(src)
        else:
            self.handle_panic(message)

    def handle_panic(self, message=None):
        with self._lock:
            players, self.players = list(self.players.values()), {}
        for p in players:
            p.stop()
        if self.engine:
            self.engine.panic()

    def handle_reload(self, message):
        """{"config": the plugin's section}: the synth, the sound and the ports
        start again with it, at once. -> .response: the status"""
        new = message.data.get("config")
        if isinstance(new, dict):
            self.config = dict(new)
        self.handle_panic()
        self.inputs.stop()
        if self.engine:
            self.engine.shutdown()
        self.engine = self._make_engine()
        self.inputs = ports.Inputs(self.config.get("inputs", ["*"]), self._on_input).start()
        for t in list(SPEAKING) + list(LISTENING):
            self.bus.remove(t, self.handle_duck)
            if self.config.get("duck", True):
                self.bus.on(t, self.handle_duck)
        self._reply(message, self.status())

    def handle_global_stop(self, message):
        if self.players:
            self.handle_panic(message)

    # ------------------------------------------------------------ keyboards
    def _on_input(self, port, msg):
        if msg.type in ("clock", "active_sensing"):
            return
        self.bus.emit(Message("ovos.midi.input", ports.message_data(port, msg)))
        if self.config.get("thru") and self.engine and hasattr(msg, "channel"):
            now, e, src = time.time(), self.engine, f"thru:{port}"
            if msg.type == "note_on" and msg.velocity > 0:
                e.add(now, "on", msg.channel, msg.note, msg.velocity, source=src)
            elif msg.type in ("note_on", "note_off"):
                e.add(now, "off", msg.channel, msg.note, source=src)
            elif msg.type == "program_change":
                e.add(now, "program", msg.channel, msg.program, source=src)
            elif msg.type == "control_change":
                e.add(now, "cc", msg.channel, msg.control, msg.value, source=src)

    def shutdown(self):
        self.handle_panic()
        self.inputs.stop()
        if self.engine:
            self.engine.shutdown()
        super().shutdown()


if sys.platform == "emscripten":      # Emkay: no threads to spare for port scanning
    ports.HAVE_PORTS = False
