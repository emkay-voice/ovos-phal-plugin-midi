# ovos-phal-plugin-midi

An [OpenVoiceOS](https://openvoiceos.org) PHAL plugin that gives the device a
MIDI instrument on its message bus.

It is part of [Emkay](https://github.com/emkay-voice/emkay), and works on any
OpenVoiceOS.

Skills can play music on it note by note, timed to the millisecond and
changed while it plays, or give it a MIDI file. It plays them on:

- **a real instrument plugged into the device**: a USB keyboard, a synth, a
  drum module, or anything else that speaks MIDI;
- **FluidSynth** with a SoundFont (real instrument samples), when it is
  installed;
- otherwise **its own synthesizer**: every General MIDI instrument and a drum
  kit, made in numpy, with a little room reverb. It needs nothing installed
  and nothing downloaded.

A keyboard plugged into the device works the other way too. What you play
goes on the bus, so a skill can follow it, and with `thru` the device plays
it.

## Why a plugin, and why software?

Sound cards used to have a synthesizer chip, but that has been gone from
ordinary computers for twenty years. A Raspberry Pi or a Mark II never had
one. Today a computer plays MIDI with software: Windows and macOS have
one built in, Linux uses FluidSynth or TiMidity. MIDI itself is alive and
well as the language of keyboards and synths, over USB.

So this plugin is the device's synth and MIDI port in one. It runs once, in
OVOS' PHAL service, and every skill can use it over the bus. That is what
makes music that **changes while it plays** possible. A skill sends the next
bar a moment ahead. When it changes its mind ("add strings"), it takes back
what it sent (`ovos.midi.cancel`) and sends the bar again. The sound never
stops or restarts, which a skill handing finished audio files to the player
cannot do.

## Install

```bash
pip install --pre ovos-phal-plugin-midi
```

That is all the built-in synth needs. Restart OVOS' PHAL service
(`systemctl --user restart ovos-phal` on ovos-installer and raspOVOS).

Optional, for more:

| | |
|---|---|
| `pip install --pre "ovos-phal-plugin-midi[ports]"` | python-rtmidi: MIDI keyboards and instruments plugged into the device |
| `pip install --pre "ovos-phal-plugin-midi[sounddevice]"` | PortAudio: the lowest latency from the built-in synth |
| `pip install --pre "ovos-phal-plugin-midi[fluidsynth]"` + `sudo apt install fluidsynth fluid-soundfont-gm` | FluidSynth with the FluidR3 SoundFont: real instrument samples |

## Configure

Nothing has to be set. To change something, in `mycroft.conf`:

```json
{
  "PHAL": {
    "ovos-phal-plugin-midi": {
      "synth": "auto",
      "audio": "auto",
      "output_port": "",
      "inputs": ["*"],
      "thru": false
    }
  }
}
```

| Setting | |
|---|---|
| `synth` | `auto` (the default) uses a MIDI port if `output_port` names one that is plugged in, else FluidSynth if it and a SoundFont are there, else the built-in synth. You can also choose `port`, `fluidsynth` or `builtin`. |
| `audio` | where the built-in synth's sound goes. `auto` (the default) tries `sounddevice`, then `paplay` (PulseAudio and PipeWire), `pw-cat`, `aplay` (plain ALSA), in that order. In a browser it uses `bus`. |
| `audio_device` | a PortAudio device for `sounddevice` (name or number) |
| `soundfont` | a `.sf2` file for FluidSynth (default: the first of the usual places, e.g. `/usr/share/sounds/sf2/FluidR3_GM.sf2`) |
| `output_port` | a MIDI output port: its name or part of it, e.g. `"Roland"` |
| `inputs` | the input ports to listen to, as names or globs (default `["*"]`: all; `[]`: none) |
| `thru` | play what comes in from a keyboard on the device's own synth (default `false`) |
| `gain`, `reverb` | the built-in synth's level (0.8) and room (`true`) |
| `sample_rate` | the built-in synth's sample rate (24000) |

## Permissions

The plugin runs inside OVOS' PHAL service, as the same user. What that user
needs depends on what you use:

- **Sound (the built-in synth or FluidSynth)**: the same access as OVOS' own
  speech.
  - On ovos-installer, raspOVOS and a Mark II, PHAL runs as the desktop or
    `ovos` user in its session, and PipeWire or PulseAudio is there.
  - If PHAL runs as a system service, it needs `XDG_RUNTIME_DIR` (or
    `PULSE_SERVER`) pointing at the user's sound server, or the user in the
    `audio` group for plain ALSA.
  - `aplay -l` or `paplay --version` as that user shows whether it can.
- **MIDI keyboards and instruments**: ALSA's sequencer (`/dev/snd/seq`) and
  the USB MIDI devices (`/dev/snd/midiC*`). These are open to the `audio`
  group (`sudo usermod -aG audio ovos`, then log in again) and to the user at
  the desk (systemd-logind). `aconnect -l` as that user lists what it can
  see.
- **Docker** (ovos-docker): the PHAL container needs the sound server's
  socket, as the audio container has it, and for MIDI devices
  `--device /dev/snd` and the `audio` group (`group_add: [audio]`).
- **Emkay** (a browser): no device access at all. The sound comes over the
  bus to Emkay's page, which plays it. Emkay sees no keyboards yet.

The plugin opens no network port and sends nothing off the device.

Anyone who can put messages on the bus can make the device play music, as
they can make it speak. What you play on a keyboard goes on the bus too
(`ovos.midi.input`), but nowhere else. Set `"inputs": []` to keep the
keyboards to yourself.

## Rights

The plugin is Apache-2.0. Its built-in instruments are made by its own code,
not recorded, so there is nothing else to license. A SoundFont you install
for FluidSynth has its own license. Debian's and Fedora's `fluid-soundfont-gm`
(FluidR3_GM) is MIT, and `timgm6mb-soundfont` is GPL-2. Both are fine to
use. The music is whoever's music it is: the plugin plays the files it is
given and keeps nothing.

## The bus

All times are wall-clock seconds (`time.time()`) on the device. All
channels are 0-15, with channel 9 as the drum kit, as in General MIDI.

| Message | Data | |
|---|---|---|
| `ovos.midi.events` | `source`, `at` (default: now + latency), `events` | play events (below) at `at` + their own time |
| `ovos.midi.cancel` | `source`, `after` (default: now) | take back that source's events from then on; answers `cancelled` (how many) |
| `ovos.midi.play` | `uri` / `path` / `data` (base64), `source`, `loop`, `transpose`, `tempo`, `mute` (channels) | play a `.mid` or `.kar` file. Answers `ovos.midi.play.started` (`at`, `duration`) or `.failed` (`error`); `ovos.midi.play.ended` when it ends |
| `ovos.midi.stop` | `source` | stop that source's file and notes; with no `source`, everything |
| `ovos.midi.panic` | | stop everything, all notes off |
| `ovos.midi.status` | | answers with the engine, `latency`, `sample_rate`, `time` (the device's clock), what is playing, the ports |
| `ovos.midi.ports` | | answers `inputs`, `outputs` |
| `ovos.midi.input` | `port`, `type`, `channel`, `note`, `velocity`, `control`, `value`, `program` | sent by the plugin: what a keyboard played |
| `ovos.midi.pcm` | `seq`, `sample_rate`, `at`, `pcm` (base64 s16le mono) | sent by the plugin with `"audio": "bus"`: the sound, for a client that plays it |

`mycroft.stop` also stops what plays from files. A skill's own notes are its
skill's to stop, with `ovos.midi.stop`.

Events, each with `t` as seconds after `at`:

```
["note", t, channel, note, velocity, seconds]   a note with its length
["on", t, channel, note, velocity]              note on (velocity 0: off)
["off", t, channel, note]
["program", t, channel, program]                 a General MIDI instrument, 0-127
["cc", t, channel, controller, value]            7 volume, 11 expression, 64 sustain pedal, 123 all notes off
["all_off", t, channel]                          channel -1: every channel
```

A bar of blues in D, starting in half a second:

```python
import time
bus.emit(Message("ovos.midi.events", {"source": "my-skill", "at": time.time() + 0.5, "events": [
    ["program", 0, 0, 0], ["program", 0, 1, 32],                       # piano, acoustic bass
    ["note", 0.0, 0, 62, 80, 1.8], ["note", 0.0, 0, 66, 80, 1.8], ["note", 0.0, 0, 72, 80, 1.8],
    ["note", 0.0, 1, 38, 100, 0.45], ["note", 0.5, 1, 42, 90, 0.45],
    ["note", 1.0, 1, 45, 90, 0.45], ["note", 1.5, 1, 47, 90, 0.45],
    ["note", 0.0, 9, 36, 110, 0.1], ["note", 0.5, 9, 38, 100, 0.1],
    ["note", 1.0, 9, 36, 110, 0.1], ["note", 1.5, 9, 38, 100, 0.1],
]}))
```

To find out how far ahead to send, ask `ovos.midi.status` for `latency` and
send at least that far ahead. A skill that sends a bar or two ahead and uses
`ovos.midi.cancel` when the music changes is always on time and still
answers quickly.

## How it works

- `synth.py` is the built-in synth: 16 channels, program, volume,
  expression and sustain pedal. A note's samples are made once
  (`instruments.py`, the same instruments as
  [ovos-skill-karaoke](https://github.com/emkay-voice/ovos-skill-karaoke)'s
  band) and kept, so playing is cheap.
- `engine.py` keeps the timeline of events and has three engines: the
  built-in synth, which places each event on its exact sample; FluidSynth;
  and a MIDI port.
- `audio.py` holds the sound outputs.
- `player.py` plays files: it hands their events to the engine a couple of
  seconds ahead, so stop is immediate.
- `ports.py` handles keyboards and instruments, and looks for newly plugged
  ones every few seconds.

## Development

```bash
pip install -e ".[test]"
pytest -q tests
```
