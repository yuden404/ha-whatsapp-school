"""AppDaemon apps: 07:15 daily schedule, 07:30 "today" list, and the daily shadow report.

WaMorning (shadow): builds the same messages as production and compares:
  - daily schedule, twice: "parity" from production's stored weekly plan (logic only, must match
    exactly) and "e2e" from the shadow's own weekly plan (model output, compared loosely).
  - morning "today" list from the real task list (read-only).
WaReport: once a day, one Telegram message with the shadow comparison results.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_store  # noqa: E402


def _compare_line(data_dir: str, line: dict):
    with open(os.path.join(data_dir, "compare", f"{datetime.now():%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **line}, ensure_ascii=False) + "\n")


class WaMorning(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        self.cfg = self.args
        wa_core.set_locale(self.cfg.get("language", "he"))
        self.data_dir = self.cfg["data_dir"]
        self.run_daily(self.run_daily_schedule, self.cfg.get("daily_time", "07:15:20"))
        self.run_daily(self.run_morning, self.cfg.get("morning_time", "07:30:20"))
        self.listen_event(self.on_prod_daily, "wa_prod_daily")
        self.listen_event(self.on_prod_morning, "wa_prod_morning")
        self.listen_event(lambda *_a, **_k: (self.run_daily_schedule({}), self.run_morning({})), "wa_shadow_morning_now")
        self.daily: dict[str, dict] = {}
        self.morning: list[str] | None = None

    def _tasks(self) -> list[dict]:
        res = self.call_service("todo/get_items", return_response=True, service_data={
            "entity_id": self.cfg["tasks_todo"], "status": "needs_action"})
        return _find_key(res, "items") or []

    def _vacation(self) -> bool:
        return bool(self.get_state(self.cfg["school_calendar_sensor"], attribute="elementary_vacation"))

    # ------------------------------------------------------------ 07:15
    def run_daily_schedule(self, _kwargs):
        day = datetime.now().strftime("%Y-%m-%d")
        self.daily = {}
        if wa_core.weekday_he(day) == "שבת" or self._vacation():
            return
        tasks = self._tasks()
        path = os.path.join(self.data_dir, "weekly_plans.json")
        shadow_plans = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                shadow_plans = json.load(f)
        for kid in self.cfg["kids"]:
            today_tasks = wa_core.tasks_for_kid(tasks, day, kid["name"])
            active, pfile = wa_core.plan_active(self.get_state(kid["pointer"]) or "", day)
            prod_plan = {"file": self.get_state(kid["sensor"], attribute="file"),
                         "days": self.get_state(kid["sensor"], attribute="days") or []}
            entry = wa_core.plan_day(prod_plan, day) if active and prod_plan["file"] == pfile else None
            parity = wa_core.daily_message(kid["name"], day, entry, today_tasks, active, kid.get("school", False))
            sp = shadow_plans.get(kid["key"]) or {}
            s_active = bool(sp) and sp.get("start", "") <= day <= sp.get("end", "")
            e2e = wa_core.daily_message(kid["name"], day, wa_core.plan_day(sp, day), today_tasks, s_active, kid.get("school", False))
            self.daily[kid["name"]] = {"parity": parity, "e2e": e2e, "entry": entry, "shadow_entry": wa_core.plan_day(sp, day)}
        self.run_in(lambda _: self._daily_unmatched(), 900)

    def on_prod_daily(self, _event, data, _kwargs):
        self.run_in(lambda _: self._compare_daily(dict(data)), 60)

    def _compare_daily(self, prod: dict):
        kid = prod.get("kid")
        s = self.daily.pop(kid, None)
        diffs = []
        if s is None:
            diffs.append("shadow_missing")
        else:
            par = s["parity"] or {}
            if par.get("kind") != prod.get("kind"):
                diffs.append(f"parity kind: shadow={par.get('kind')} prod={prod.get('kind')}")
            elif par.get("kind") == "no_plan":
                if par.get("body") != prod.get("body"):
                    diffs.append("parity text differs")
            elif (par.get("title"), par.get("body")) != (prod.get("title"), prod.get("body")):
                diffs.append("parity text differs")
            se, pe = s["shadow_entry"] or {}, s["entry"] or {}
            if len(se.get("lessons") or []) != len(pe.get("lessons") or []):
                diffs.append(f"e2e lessons: shadow={len(se.get('lessons') or [])} prod={len(pe.get('lessons') or [])}")
            sb, pb = set(se.get("bring") or []), set(pe.get("bring") or [])
            if pb and len(sb & pb) / len(pb) < 0.5:
                diffs.append(f"e2e bring overlap {len(sb & pb)}/{len(pb)}")
        _compare_line(self.data_dir, {"part": "daily", "kid": kid, "ok": not diffs, "diffs": diffs})

    def _daily_unmatched(self):
        for kid, s in list(self.daily.items()):
            if s["parity"] is not None:  # shadow would have sent something, production did not
                _compare_line(self.data_dir, {"part": "daily", "kid": kid, "ok": False, "diffs": ["only_shadow"]})
        self.daily = {}

    # ------------------------------------------------------------ 07:30
    def run_morning(self, _kwargs):
        now = datetime.now()
        no_school = now.strftime("%w") == "6" or self.get_state(self.cfg["issur_melacha_sensor"]) == "on" or self._vacation()
        self.morning = [i["summary"] for i in wa_core.morning_items(self._tasks(), now.strftime("%Y-%m-%d"), no_school)]

    def on_prod_morning(self, _event, data, _kwargs):
        items = data.get("items")
        items = json.loads(items) if isinstance(items, str) else (items or [])
        self.run_in(lambda _: self._compare_morning(items), 60)

    def _compare_morning(self, prod_items: list[str]):
        s = self.morning
        diffs = []
        if s is None:
            diffs.append("shadow_missing")
        elif s != prod_items:
            diffs.append(f"only_shadow={[x for x in s if x not in prod_items]} only_prod={[x for x in prod_items if x not in s]}")
        _compare_line(self.data_dir, {"part": "morning", "ok": not diffs, "diffs": diffs})
        self.morning = None


class WaNight(hass.Hass):
    """06:00: one message with alert-worthy messages that arrived at night (shadow: compare only).
    Unlike the old automation, messages already alerted during the day are not repeated."""

    def initialize(self):
        importlib.reload(wa_core)
        self.cfg = self.args
        self.data_dir = self.cfg["data_dir"]
        self.run_daily(self.run_night, self.cfg.get("time", "06:00:30"))
        self.listen_event(self.on_prod_night, "wa_prod_night")
        self.msg: str | None = None

    def run_night(self, _kwargs):
        q = wa_store.JsonlQueue(os.path.join(self.data_dir, "queue.jsonl")).read()
        self.msg = wa_core.night_alert_message([x for x in q if not x.get("alerted")])
        # non-shadow (later): read receipts per chat, then send self.msg if not empty

    def on_prod_night(self, _event, data, _kwargs):
        prod = set(filter(None, str(data.get("msg") or "").splitlines()))
        shadow = set(filter(None, (self.msg or "").splitlines()))
        diffs = []
        if self.msg is None:
            diffs.append("shadow_missing")
        elif prod != shadow:
            diffs.append(f"only_shadow={len(shadow - prod)} only_prod={len(prod - shadow)} (prod repeats daytime alerts)")
        _compare_line(self.data_dir, {"part": "night", "ok": not diffs, "diffs": diffs})
        self.msg = None


class WaReport(hass.Hass):
    """One Telegram message a day with the shadow comparison results."""

    def initialize(self):
        self.cfg = self.args
        self.run_daily(self.report, self.cfg.get("time", "21:45:00"))
        self.listen_event(lambda *_a, **_k: self.report({}), "wa_shadow_report_now")

    def report(self, _kwargs):
        p = os.path.join(self.cfg["data_dir"], "compare", f"{datetime.now():%Y-%m-%d}.jsonl")
        lines = []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                lines = [json.loads(line) for line in f if line.strip()]
        parts: dict[str, list] = {}
        for line in lines:
            parts.setdefault(line.get("part", "?"), []).append(line)
        names = {"ingest": "קליטת הודעות", "summary": "סיכומים", "daily": "מערכת 07:15", "morning": "רשימת הבוקר",
                 "alert": "התראות מיידיות", "night": "התראות הלילה"}
        msg = "🔬 צל AppDaemon — השוואה יומית\n"
        if not lines:
            msg += "אין נתונים להיום (לא הגיעו הודעות / לא רץ כלום)."
        for part, ls in parts.items():
            ok = sum(1 for x in ls if x.get("ok"))
            msg += f"\n{'✅' if ok == len(ls) else '⚠️'} {names.get(part, part)}: {ok}/{len(ls)} זהים"
            for bad in [x for x in ls if not x.get("ok")][:3]:
                msg += "\n   • " + "; ".join(bad.get("diffs") or [])[:160]
        self.call_service("notify/send_message", entity_id=self.cfg["notify_entity"], message=wa_core.telegram_safe(msg))


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
