"""The package is installed (pip install -e .)."""
import json
import threading

import pytest


class FakeBus:
    """Enough of a MessageBusClient: on/emit, and a record of what was emitted."""

    def __init__(self):
        self.handlers = {}
        self.emitted = []
        self.lock = threading.Lock()

    def on(self, msg_type, handler):
        self.handlers.setdefault(msg_type, []).append(handler)

    def remove(self, msg_type, handler):
        if handler in self.handlers.get(msg_type, []):
            self.handlers[msg_type].remove(handler)

    def once(self, msg_type, handler):
        self.on(msg_type, handler)

    def emit(self, message):
        json.dumps(message.data)                 # what goes on the bus must be JSON
        with self.lock:
            self.emitted.append(message)
        for h in list(self.handlers.get(message.msg_type, [])):
            h(message)

    def types(self):
        with self.lock:
            return [m.msg_type for m in self.emitted]

    def last(self, msg_type):
        with self.lock:
            return next((m for m in reversed(self.emitted) if m.msg_type == msg_type), None)


@pytest.fixture
def bus():
    return FakeBus()
