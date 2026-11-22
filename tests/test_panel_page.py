"""The page this plugin has in ovos-control-panel, described in
controlpanel.json: the file, its strings in every language, and the status
it reads."""
import json
import re
from pathlib import Path

import pytest

import ovos_phal_plugin_midi as m
from ovos_phal_plugin_midi import MidiPlugin

ROOT = Path(m.__file__).parent
TEXT = (ROOT / "controlpanel.json").read_text(encoding="utf-8")
PAGE = json.loads(TEXT)
LANGS = {"ca", "da", "de", "en", "es", "eu", "fr", "gl", "it", "nl", "pt"}


def test_the_page_names_files_that_exist_and_its_own_messages():
    assert PAGE["controlpanel"] == 1 and PAGE["id"] == "midi" and PAGE["group"] == "music"
    assert (ROOT / PAGE["icon"]).is_file()
    sent = re.findall(r'"(?:query|send|reload)": "([^"]+)"', TEXT)
    assert sent and all(s.startswith(PAGE["bus"]) for s in sent)


def test_the_page_speaks_every_language():
    files = {p.parent.name.split("-")[0]: json.loads(p.read_text(encoding="utf-8"))
             for p in (ROOT / "locale").glob("*/controlpanel.json")}
    assert set(files) == LANGS
    keys = set(files["en"])
    for lang, table in files.items():
        assert set(table) == keys, lang
        assert all(text.strip() for text in table.values()), lang
    used = set(re.findall(r'"(midi\.[\w.]+)"', TEXT)) | set(re.findall(r"@(midi\.[\w.]+)\}", TEXT))
    labels = {k for k in keys if any(k == u or k.startswith(u + ".") for u in used)}
    assert used - {"midi.engine"} <= keys and keys - labels <= {"nav.group.music"}


def test_the_panel_reads_it():
    described = pytest.importorskip("ovos_webui.described")
    page, refs = described.check(PAGE, "ovos-phal-plugin-midi")
    assert [b["type"] for b in page["blocks"]] == ["text", "status", "form"]
    form = refs["b2.form"]
    assert described.form_values(form, {"inputs": False, "synth": "port"}) == {"inputs": [], "synth": "port"}


def test_the_status_has_what_the_page_shows():
    plugin = MidiPlugin(bus=__import__("ovos_utils.fakebus", fromlist=["FakeBus"]).FakeBus(),
                        config={"audio": "null", "synth": "builtin", "inputs": []})
    try:
        s = plugin.status()
        assert s["engine"] == "builtin" and isinstance(s["latency_ms"], int)
        assert "outputs" in s["ports"] and "available" in s["ports"]
    finally:
        plugin.shutdown()
