"""The MIDI page's API. The settings are saved in mycroft.conf (PHAL ->
ovos-phal-plugin-midi) through the panel, with its backups, and then handed
to the plugin, which starts its synth and ports again with them at once."""
from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

PLUGIN = "ovos-phal-plugin-midi"
STATUS = "ovos.midi.status"
RELOAD = "ovos.midi.reload"
TIMEOUT = 8.0
SOURCE = "control-panel"

#: what the test plays: a C major chord on a piano, then a drum fill
TESTS = {
    "chord": [["program", 0, 0, 0]] + [["note", 0.02 + i * 0.12, 0, n, 90, 1.6] for i, n in enumerate((60, 64, 67, 72))],
    "drums": [["note", i * 0.25, 9, n, 100, 0.2] for i, n in enumerate((36, 42, 38, 42, 36, 36, 38, 49))],
}


class Settings(BaseModel):
    synth: Literal["auto", "builtin", "fluidsynth", "port"] = "auto"
    audio: Literal["auto", "sounddevice", "paplay", "pw-cat", "aplay"] = "auto"
    soundfont: str = Field("", max_length=500)
    output_port: str = Field("", max_length=200)
    listen: bool = True
    thru: bool = False
    duck: bool = True
    duck_volume: float = Field(0.3, ge=0, le=1)
    reverb: bool = True


class Test(BaseModel):
    sound: Literal["chord", "drums"] = "chord"


def ask(r, msg_type: str, data: Optional[dict] = None, timeout: float = TIMEOUT) -> Optional[dict]:
    from ovos_bus_client.message import Message
    bus = r.need_bus()
    message = Message(msg_type, data or {})
    try:
        from ovos_webui import buswait
        reply = buswait.wait_for_response(bus, message, timeout=timeout)
    except ImportError:  # an older panel: the bus client's own wait
        reply = bus.wait_for_response(message, timeout=timeout)
    return None if reply is None else reply.data


def tell(r, msg_type: str, data: dict) -> None:
    from ovos_bus_client.message import Message
    r.need_bus().emit(Message(msg_type, data, {"skill_id": SOURCE}))


def save(r, body: Settings) -> dict[str, Any]:
    """Save the settings in the user's mycroft.conf: the defaults are left
    out, so the file says only what was changed. The section as it is now."""
    values = {"synth": body.synth, "audio": body.audio, "soundfont": body.soundfont.strip(),
              "output_port": body.output_port.strip(), "inputs": ["*"] if body.listen else [],
              "thru": body.thru, "duck": body.duck, "duck_volume": round(body.duck_volume, 2),
              "reverb": body.reverb}
    defaults = {"synth": "auto", "audio": "auto", "soundfont": "", "output_port": "", "inputs": ["*"],
                "thru": False, "duck": True, "duck_volume": 0.3, "reverb": True}

    def change(user: dict) -> None:
        mine = dict((user.setdefault("PHAL", {})).get(PLUGIN) or {})
        for key, value in values.items():
            if value == defaults[key]:
                mine.pop(key, None)
            else:
                mine[key] = value
        if mine:
            user["PHAL"][PLUGIN] = mine
        else:
            user["PHAL"].pop(PLUGIN, None)

    r.config.mutate(change, bus=r.bus())
    return dict((r.config.section("PHAL") or {}).get(PLUGIN) or {})


def routes(r) -> None:
    @r.api.get("/midi")
    def api_midi() -> dict[str, Any]:
        answer = ask(r, STATUS)
        if answer is None:
            raise HTTPException(504, "the MIDI plugin did not answer; is OVOS' PHAL service running?")
        return answer

    @r.api.post("/midi")
    def api_midi_save(body: Settings) -> dict[str, Any]:
        section = save(r, body)
        answer = ask(r, RELOAD, {"config": section})
        if answer is None:
            return {"saved": True, "restart": True}
        return {"saved": True, "restart": False, **answer}

    @r.api.post("/midi/test")
    def api_midi_test(body: Test) -> dict[str, Any]:
        tell(r, "ovos.midi.events", {"source": SOURCE, "events": TESTS[body.sound]})
        return {"ok": True}

    @r.api.post("/midi/stop")
    def api_midi_stop() -> dict[str, Any]:
        tell(r, "ovos.midi.panic", {})
        return {"ok": True}
