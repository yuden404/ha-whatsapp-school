"""A minimal stand-in for appdaemon's Hass, so the apps can be tested without AppDaemon or Home Assistant.

make(AppClass, args, ...) builds a copy of the app class on top of FakeHass (no sys.modules patching,
so it is safe to run inside AppDaemon too). Service calls are answered from 'services', a dict of
"domain/service" -> response or callable(service_data) -> response.
"""
from __future__ import annotations

import importlib
import sys
import types


def _ensure_appdaemon_stub():
    """Outside AppDaemon the package is missing; give the apps an importable base class."""
    try:
        import appdaemon.plugins.hass.hassapi  # noqa: F401
    except ImportError:
        stub = types.ModuleType("appdaemon.plugins.hass.hassapi")
        stub.Hass = type("Hass", (), {})
        for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["appdaemon.plugins.hass.hassapi"] = stub
        sys.modules["appdaemon.plugins.hass"].hassapi = stub


class FakeHass:
    def __init__(self, args: dict, states: dict | None = None, services: dict | None = None):
        self.args = args
        self.states = states or {}          # entity_id -> {"state": ..., "attributes": {...}}
        self.services = services or {}      # "domain/service" -> response | callable
        self.calls: list[tuple[str, dict]] = []
        self.events: list[tuple[str, dict]] = []
        self.logs: list[str] = []
        self.timers: list[tuple[float, object]] = []
        self.set_states: dict[str, dict] = {}

    # --- what the apps use from AppDaemon
    def log(self, msg, *a, **k):
        self.logs.append(str(msg))

    def get_state(self, entity_id, attribute=None, **k):
        e = self.states.get(entity_id)
        if e is None:
            return None
        if attribute == "all":
            return e
        if attribute:
            return e.get("attributes", {}).get(attribute)
        return e.get("state")

    def set_state(self, entity_id, state=None, attributes=None, **k):
        self.set_states[entity_id] = {"state": state, "attributes": attributes or {}}

    def call_service(self, service, **data):
        self.calls.append((service, data))
        resp = self.services.get(service)
        return resp(data) if callable(resp) else resp

    def fire_event(self, event, **data):
        self.events.append((event, data))

    def listen_event(self, cb, event, **k):
        self.events.append(("listen", {"event": event}))

    def run_in(self, cb, delay, **k):
        self.timers.append((delay, cb))

    def run_daily(self, cb, when, **k):
        self.timers.append((when, cb))

    def render_template(self, tpl, **k):
        return self.services.get("render_template", lambda d: "")({"template": tpl})

    def fire_timers(self):
        """Run every pending run_in callback once (synchronously)."""
        pending, self.timers = self.timers, []
        for _, cb in pending:
            cb({})


def make(module_name: str, class_name: str, args: dict, states=None, services=None):
    """Instantiate app class_name from module_name on top of FakeHass and call initialize()."""
    _ensure_appdaemon_stub()
    mod = importlib.import_module(module_name)  # no reload: inside AppDaemon the real apps are running
    cls = getattr(mod, class_name)
    body = {k: v for k, v in vars(cls).items() if k not in ("__dict__", "__weakref__")}
    fake_cls = type(class_name, (FakeHass,), body)
    app = fake_cls(args, states, services)
    app.initialize()
    return app
