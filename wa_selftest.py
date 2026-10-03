"""AppDaemon app: self-tests for the WhatsApp pipeline.

Runs when the app (re)loads, i.e. after any code change in this folder (AppDaemon
reloads changed apps), and after Home Assistant restarts.
1. unit:        every test_* in wa_tests/test_wa_core.py (pure Python)
2. parity:      Python port vs the Jinja macros currently in production
3. integration: live checks against HA / WAHA / Gemini
Telegram only on failure (or notify_always: true in apps.yaml).
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402


class WaSelfTest(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        self.cfg = self.args
        self.listen_event(self._on_ha_start, "homeassistant_started")
        self.listen_event(self._on_ha_start, "plugin_started")
        delay = int(self.cfg.get("start_delay", 20))
        self.run_in(lambda _: self.run_all("reload"), delay)

    def _on_ha_start(self, *_a, **_k):
        self.run_in(lambda _: self.run_all("ha_start"), 300)

    # ------------------------------------------------------------ runner
    def run_all(self, reason: str):
        results: list[tuple[str, str, bool, str]] = []

        def rec(group, name, fn):
            try:
                ok, detail = fn()
            except AssertionError as e:
                ok, detail = False, str(e) or "assert"
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"
            results.append((group, name, bool(ok), str(detail)[:200]))

        # 1. unit
        for modname in ("wa_tests.test_wa_core", "wa_tests.test_apps"):
            try:
                mod = importlib.import_module(modname)
                importlib.reload(mod)
            except Exception as e:  # noqa: BLE001  a broken test module is itself a failed test, not a crash
                results.append(("unit", modname, False, f"import failed: {type(e).__name__}: {e}"[:200]))
                continue
            for n in sorted(dir(mod)):
                fn = getattr(mod, n)
                if n.startswith("test_") and callable(fn):
                    rec("unit", n, lambda f=fn: (f() or True, ""))

        # 2. parity with production Jinja
        for name, fn in self._parity_cases():
            rec("parity", name, fn)

        # 3. integration
        for name, fn in self._integration_cases():
            rec("integration", name, fn)

        failed = [r for r in results if not r[2]]
        summary = {g: f"{sum(1 for r in results if r[0]==g and r[2])}/{sum(1 for r in results if r[0]==g)}"
                   for g in ("unit", "parity", "integration")}
        self.log(f"selftest[{reason}] {summary} failed={[(r[0], r[1], r[3]) for r in failed]}")
        self.set_state("sensor.wa_selftest", state="ok" if not failed else "failed",
                       attributes={"summary": summary, "failed": [f"{r[0]}/{r[1]}: {r[3]}" for r in failed],
                                   "reason": reason, "ran_at": datetime.now().isoformat(timespec="seconds")})
        if failed or self.cfg.get("notify_always"):
            head = "✅" if not failed else "❌"
            total_ok = sum(1 for r in results if r[2])
            msg = f"{head} בדיקות AppDaemon (וואטסאפ): {total_ok}/{len(results)} עברו · " + \
                  " · ".join(f"{k} {v}" for k, v in summary.items())
            for g, n, _, d in failed[:10]:
                msg += f"\n• {g}/{n} — {d[:120]}"
            self.call_service("notify/send_message", entity_id=self.cfg["notify_entity"], message=wa_core.telegram_safe(msg))

    # ------------------------------------------------------------ parity
    def _jinja(self, macro_call: str):
        tpl = ("{% from 'whatsapp.jinja' import wa_dedupe, wa_monitored, wa_safe_path, "
               "wa_day_label, wa_next_school_day, wa_morning_items %}{{ " + macro_call + " }}")
        out = self.render_template(tpl)  # AppDaemon already literal_eval()s the result
        if isinstance(out, str):
            try:
                return json.loads(out)
            except ValueError:
                return out
        return out

    def _parity_cases(self):
        groups = self.cfg["groups"]
        lists = [["קסם וחברים 1", "חוברת קסם וחברים 1", "מחברת", "מחברת שפה"],
                 ["סווטשרט", "סווטשרט ", "כובע (לשעה 5)", "כובע"],
                 ["חוברת שבילים 5", "ספר שבילים 5", "סרגל"], ["מים", "בקבוק מים"], ["אב", "אבטיח"]]
        items = [{"summary": "א", "due": "2026-10-03", "description": "x · ללא תאריך בהודעה"},
                 {"summary": "ב", "due": "2026-10-03", "description": "x"},
                 {"summary": "ג", "due": "2026-10-02", "description": "x · ללא תאריך בהודעה"}]
        cases = []
        for i, lst in enumerate(lists):
            cases.append((f"dedupe_{i}", lambda l=lst: self._eq(wa_core.dedupe(l), self._jinja(f"wa_dedupe({json.dumps(l, ensure_ascii=False)})"))))
        for d, r in [("2026-10-04", "2026-10-01"), ("2026-10-03", "2026-10-02"), ("", "2026-10-02")]:
            cases.append((f"day_label_{d or 'empty'}", lambda d=d, r=r: self._eq(wa_core.day_label(d, r), self._jinja(f"wa_day_label('{d}','{r}')"))))
        for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
            cases.append((f"next_school_day_{d}", lambda d=d: self._eq(wa_core.next_school_day(d), self._jinja(f"wa_next_school_day('{d}')"))))
        for day, ns in (("2026-10-03", True), ("2026-10-04", False), ("2026-10-03", False)):
            cases.append((f"morning_{day}_{ns}", lambda day=day, ns=ns: self._eq(
                [x["summary"] for x in wa_core.morning_items(items, day, ns)],
                [x["summary"] for x in self._jinja(f"wa_morning_items({json.dumps(items, ensure_ascii=False)}, '{day}', {str(ns).lower()})")])))
        for p in ("whatsapp/a.pdf", "whatsapp/תוכנית.pdf", "whatsapp/../x"):
            cases.append((f"safe_path_{p[-6:]}", lambda p=p: self._eq(wa_core.safe_path(p), self._jinja(f"wa_safe_path('{p}')"))))
        extra = self.get_state(self.cfg["groups_helper"]) or ""
        cases.append(("monitored_live", lambda: self._eq(sorted(wa_core.monitored_groups(groups, extra)),
                                                         sorted(self._jinja(f"wa_monitored('{extra}')")))))
        return cases

    @staticmethod
    def _eq(a, b):
        return a == b, "" if a == b else f"python={a!r} jinja={b!r}"

    # ------------------------------------------------------------ integration
    def _integration_cases(self):
        c = self.cfg
        now = datetime.now().astimezone()

        def state(e):
            return self.get_state(e)

        def waha_session():
            with open(c["waha_header_file"], encoding="utf-8") as fh:  # "X-Api-Key: ..." kept outside the repo
                k, v = fh.read().strip().split(":", 1)
            req = urllib.request.Request(c["waha_url"].rstrip("/") + "/api/sessions/default",
                                         headers={k.strip(): v.strip()})
            with urllib.request.urlopen(req, timeout=10) as r:
                s = json.load(r)
            ev = s["config"]["webhooks"][0]["events"]
            return s["status"] == "WORKING" and "message" in ev and "session.status" in ev, s["status"]

        def last_webhook():
            v = state(c["last_webhook_helper"])
            dt = datetime.fromisoformat(v).astimezone() if v and v not in ("unknown", "unavailable") else None
            return bool(dt and now - dt < timedelta(hours=48)), v

        def queue_not_stuck():
            st = self.get_state(c["queue_todo"], attribute="all") or {}
            n = int(st.get("state") or 0)
            lc = datetime.fromisoformat(st.get("last_changed")) if st.get("last_changed") else now
            return n == 0 or now - lc < timedelta(hours=26), f"{n} items"

        def automations_on():
            bad = [a for a in c["automations"] if state(a) != "on"]
            return not bad, bad

        def weekly_structure():
            bad = []
            for s in c["weekly_sensors"]:
                days = self.get_state(s, attribute="days")
                if state(s) in (None, "unknown", "unavailable", ""):
                    continue
                if not isinstance(days, list) or any(not {"date", "lessons", "bring"} <= set(d) for d in days):
                    bad.append(s)
            return not bad, bad

        def gemini_structured():
            data = None
            for _attempt in range(2):
                res = self.call_service(
                    "ai_task/generate_data", return_response=True, hass_timeout=90,
                    # entity_id inside service_data: ai_task wants a string, AppDaemon would turn the kwarg into a target list
                    service_data={
                        "entity_id": c["ai_task_entity"], "task_name": "appdaemon_selftest",
                        "instructions": 'מתוך ההודעה הבאה מקבוצת הורים, חלץ משימות לילד/ה: "מחר יש גיאומטריה, נא לשלוח סרגל." מלא tasks עם action קצר בניסוח שלך.',
                        "structure": {"tasks": {"required": True, "selector": {"object": {"multiple": True, "fields": {
                            "action": {"required": True, "selector": {"text": {}}}}}}}}})
                data = _find_key(res, "data")
                if isinstance(data, dict) and data.get("tasks"):
                    break
                time.sleep(15)
            ok = isinstance(data, dict) and any("סרגל" in str(t.get("action", "")) for t in data.get("tasks", []))
            return ok, data

        return [
            ("monitored_groups_complete", lambda: (len(wa_core.monitored_groups(c["groups"], state(c["groups_helper"]))) >= len(c["groups"]), "")),
            ("waha_running", lambda: (state(c["waha_running"]) == "on", state(c["waha_running"]))),
            ("waha_session_working", waha_session),
            ("last_webhook_lt_48h", last_webhook),
            ("queue_not_stuck", queue_not_stuck),
            ("automations_on", automations_on),
            ("weekly_plan_structure", weekly_structure),
            ("gemini_structured_output", gemini_structured),
        ]


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None
