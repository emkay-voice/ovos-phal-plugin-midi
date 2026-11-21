"""The page this plugin adds to ovos-control-panel: its description, its
strings in every language, and its routes talking to the plugin."""
import json
import re
import sys
from pathlib import Path

import pytest

from ovos_phal_plugin_midi import MidiPlugin, panel

PANEL = Path(panel.__file__).parent
LANGS = {"ca", "da", "de", "en", "es", "eu", "fr", "gl", "it", "nl", "pt"}


def test_the_page_description_names_files_that_exist():
    page = panel.PAGE
    assert page["api_version"] == 1 and page["id"] == "midi" and page["group"] == "music"
    assert panel.MUSIC in page["groups"]
    for key in ("html", "icon"):
        assert (PANEL / page[key]).is_file(), key


def test_the_page_speaks_every_language():
    files = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (PANEL / "i18n").glob("*.json")}
    assert set(files) == LANGS
    keys = set(files["en"])
    for lang, table in files.items():
        assert set(table) == keys, lang
        assert all(text.strip() for text in table.values()), lang
    html = (PANEL / "page.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n="([^"]+)"', html)) | set(re.findall(r'"(midi\.[\w.]+)":', html))
    assert used <= keys and keys - used <= {"midi.title", "nav.group.music"}


pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    from ovos_utils.fakebus import FakeBus

    monkeypatch.setitem(sys.modules, "ovos_webui", None)   # the bus client's own wait
    bus = FakeBus()
    plugin = MidiPlugin(bus=bus, config={"audio": "null", "synth": "builtin", "inputs": []})
    user = {"PHAL": {"ovos-phal-plugin-midi": {"audio": "null", "inputs": []}}}

    class Config:
        @staticmethod
        def mutate(change, bus=None):
            change(user)

        @staticmethod
        def section(name):
            return json.loads(json.dumps(user.get(name) or {}))

    class R:
        api = APIRouter(prefix="/api")
        privileged = APIRouter(prefix="/api")
        config = Config

        @staticmethod
        def need_bus():
            return bus

        @staticmethod
        def bus():
            return bus

    panel.routes(R)
    app = FastAPI()
    app.include_router(R.api)
    app.include_router(R.privileged)
    with TestClient(app) as c:
        yield c, plugin, user, bus
    plugin.shutdown()


def test_the_page_shows_the_status(client):
    c, _, _, _ = client
    s = c.get("/api/midi").json()
    assert s["engine"] == "builtin" and "settings" in s


def test_saving_writes_only_what_was_changed_and_takes_effect(client, monkeypatch):
    c, plugin, user, _ = client
    import ovos_phal_plugin_midi as m
    real = m.make_engine
    monkeypatch.setattr(m, "make_engine", lambda cfg, bus=None: real({**cfg, "audio": "null"}, bus))
    r = c.post("/api/midi", json={"synth": "builtin", "listen": True, "thru": True, "duck_volume": 0.5})
    assert r.status_code == 200, r.text
    mine = user["PHAL"]["ovos-phal-plugin-midi"]
    assert mine["synth"] == "builtin" and mine["thru"] is True and mine["duck_volume"] == 0.5
    assert "inputs" not in mine and "duck" not in mine, "the defaults are left out"
    assert r.json()["settings"]["thru"] is True and plugin.config["thru"] is True


def test_a_test_sound_and_stop(client):
    c, plugin, _, bus = client
    seen = []
    bus.on("ovos.midi.events", lambda m: seen.append(m))
    assert c.post("/api/midi/test", json={"sound": "drums"}).json() == {"ok": True}
    assert seen and c.post("/api/midi/stop").json() == {"ok": True}
    assert c.post("/api/midi/test", json={"sound": "kazoo"}).status_code == 422
