"""The MIDI page in ovos-control-panel.

The control panel finds this page through the ``opm.control_panel.page``
entry point (see pyproject.toml and the panel's docs/pages.md). It shows what
plays the music (the built-in synth, FluidSynth or an instrument on a MIDI
port), the keyboards plugged in, sets them up, and plays a test. Nothing here
is loaded by the plugin itself, so the plugin needs no web packages; the
panel brings those.
"""


def routes(r):
    # Imported here, so reading PAGE (the panel's registry, this plugin's
    # tests) needs no web framework; only the panel calls this.
    from ovos_phal_plugin_midi.panel.api import routes as add
    add(r)


#: the panel's Music group: music you make with the device. Newer panels have
#: it themselves; this brings it to older ones, and its name to the
#: languages the panel itself has no strings for
MUSIC = {"id": "music", "title": {"key": "nav.group.music", "label": "Music"},
         "order": 6, "level": "everyday"}

PAGE = {
    "api_version": 1,
    "id": "midi",
    "path": "/midi",
    "title": {"key": "midi.title", "label": "MIDI and instruments"},
    # the instrument the music skills play on: last in Music, as their setup
    "group": "music",
    "groups": [MUSIC],
    "order": 90,
    "needs": ["ovos-phal-plugin-midi"],
    "html": "page.html",
    "icon": "icon.svg",
    "i18n": "i18n",
    "routes": routes,
}
