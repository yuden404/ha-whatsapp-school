"""AppDaemon app: WhatsApp ingest (webhook -> filter -> media -> file read -> weekly plan).

Shadow mode (shadow: true): receives the same raw webhook payload that production gets
(event wa_shadow_webhook, fired by the production automation), does all the work, but
writes only to its own store and never notifies, marks read or touches HA entities.
Production reports what it did via event wa_prod_ingest; both sides are compared per
message id and logged to <data_dir>/compare/YYYY-MM-DD.jsonl.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import time
import sys
import urllib.request
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402


class WaIngest(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        self.shadow = bool(c.get("shadow", True))
        self.data_dir = c["data_dir"]
        os.makedirs(os.path.join(self.data_dir, "compare"), exist_ok=True)
        pdir = os.path.join(HERE, "prompts")
        self.prompts = {k: wa_core.load_prompt(pdir, k, "file_read") for k in ("file_read", "file_read_retry")}
        self.pending: dict[str, dict] = {}  # msg id -> {"shadow": {...}, "prod": {...}}
        self.listen_event(self.on_webhook, "wa_shadow_webhook")
        self.listen_event(self.on_prod, "wa_prod_ingest")
        self.listen_event(self.on_prod_alert, "wa_prod_alert")
        self.alerts: dict[str, dict] = {}  # msg id -> shadow day-alert decision
        self.log(f"WaIngest ready (shadow={self.shadow})")

    # ------------------------------------------------------------ webhook
    def on_webhook(self, _event, data, _kwargs):
        raw = data.get("json") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        ev, p = raw.get("event", ""), raw.get("payload") or {}
        monitored = wa_core.monitored_groups(self.cfg["groups"], self.get_state(self.cfg["groups_helper"]) or "")
        ok, reason = wa_core.accept_message(ev, p, monitored)
        if not ok:
            return
        mid = str(p.get("id") or "")
        rec = {"id": mid, "chat": p.get("from"), "kind": wa_core.message_kind(p.get("body"), self.cfg.get("alert_names", [])),
               "media": None, "ftext_len": 0, "weekly": False, "start": "", "end": "", "ndays": 0, "error": ""}
        sender, item = wa_core.queue_item_text(p)
        ftext, mime = "", (p.get("media") or {}).get("mimetype") or ""
        try:
            rel = wa_core.media_rel_path(p, root=self.cfg["media_root"])
            if rel:
                self._download(p["media"]["url"], rel)
                rec["media"] = rel
                fr = self._read_file(rel, p["media"]["mimetype"])
                ftext = fr.get("text", "")
                rec.update(ftext_len=len(fr.get("text", "")), weekly=fr["weekly"], start=fr["start"],
                           end=fr["end"], ndays=len(fr["days"]))
                if fr["weekly"]:
                    self._store_plan(p.get("from"), rel, fr)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
        daytime = wa_core.is_daytime(datetime.now())
        label = self.cfg.get("alert_group_labels", {}).get(str(p.get("from") or "").split("@")[0], str(p.get("from") or "").split("@")[0])
        alert = wa_core.day_alert(rec["kind"], label, sender, p.get("body") or "") if daytime else None
        if alert:
            self.alerts[mid] = alert
            self.run_in(lambda _: self._alert_timeout(mid), 600)
        # non-shadow (later): after 30-120 s send the read receipt (daytime only) and the alert
        self._enqueue({"id": mid, "ts": p.get("timestamp") or 0, "chat": p.get("from"), "sender": sender,
                       "item": item, "ftext": ftext, "media": rec["media"], "mime": mime,
                       "kind": rec["kind"], "alerted": bool(alert)})
        self._side(mid, "shadow", rec)

    def _enqueue(self, q: dict):
        """Shadow queue consumed by wa_summary (production uses a HA todo list)."""
        with open(os.path.join(self.data_dir, "queue.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    def _download(self, url: str, rel: str):
        url = url.replace("http://localhost:3000", self.cfg["waha_url"].rstrip("/"))
        k, v = open(self.cfg["waha_header_file"]).read().strip().split(":", 1)
        dest = os.path.join("/media", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={k.strip(): v.strip()})
                with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return
            except Exception:  # noqa: BLE001
                if attempt:
                    raise
                time.sleep(5)

    def _read_file(self, rel: str, mime: str) -> dict:
        """One Gemini call: text + weekly detection + days. Retry with paraphrase prompt on failure."""
        if not wa_core.safe_path(rel):
            return {"text": "", "weekly": False, "start": "", "end": "", "days": []}
        today = datetime.now().strftime("%Y-%m-%d")
        data = None
        for key in ("file_read", "file_read_retry"):
            pr = self.prompts[key]
            res = self.call_service(
                "ai_task/generate_data", return_response=True, hass_timeout=180,
                service_data={"entity_id": self.cfg["ai_task_entity"], "task_name": f"shadow_{key}",
                              "instructions": wa_core.fill(pr["instructions"], {
                                  "TODAY": today, "OUTPUT_LANGUAGE": self.cfg.get("output_language", "Hebrew")}),
                              "structure": pr["structure"],
                              "attachments": [{"media_content_id": f"media-source://media_source/local/{rel}",
                                               "media_content_type": mime}]})
            data = _find_key(res, "data")
            if isinstance(data, dict) and data.get("text"):
                break
        data = data if isinstance(data, dict) else {}
        days = []
        for d in sorted([x for x in data.get("days") or [] if x.get("date")], key=lambda x: x["date"]):
            lessons = d.get("lessons") or []
            days.append({"date": d["date"], "weekday": d.get("weekday", ""),
                         "no_school": bool(d.get("no_school")) and not lessons, "hours": d.get("hours", ""),
                         "lessons": lessons, "bring": wa_core.dedupe(d.get("bring") or []),
                         "notes": wa_core.dedupe(d.get("notes") or [])})
        return {"text": str(data.get("text") or "")[:6000], "weekly": bool(data.get("weekly_schedule")) and bool(days),
                "start": data.get("start") or "", "end": data.get("end") or "", "days": days}

    def _store_plan(self, chat, rel, fr):
        child = wa_core.child_for_chat(self.cfg["groups"], chat)
        if not child or fr["end"] < datetime.now().strftime("%Y-%m-%d"):
            return
        path = os.path.join(self.data_dir, "weekly_plans.json")
        plans = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        cur = plans.get(child)
        if cur and fr["start"] < cur.get("start", ""):
            return
        plans[child] = {"file": rel, "start": fr["start"], "end": fr["end"], "days": fr["days"]}
        if self.shadow:
            json.dump(plans, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        # non-shadow (later): set input_text + fire weekly_plan_update + notify

    # ------------------------------------------------------------ compare
    def on_prod_alert(self, _event, data, _kwargs):
        mid = str(data.get("id") or "")
        s = self.alerts.pop(mid, None)
        p = {"title": data.get("title"), "text": data.get("text")}
        diffs = []
        if s is None:
            diffs.append("only_prod")
        else:
            if s["title"] != p["title"]:
                diffs.append(f"title: shadow={s['title']} prod={p['title']}")
            if s["text"] != p["text"]:
                diffs.append("text differs")
        self._log_compare("alert", mid, diffs)

    def _alert_timeout(self, mid: str):
        if self.alerts.pop(mid, None):
            self._log_compare("alert", mid, ["only_shadow"])

    def _log_compare(self, part: str, mid: str, diffs: list):
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "part": part, "id": mid[-24:], "ok": not diffs, "diffs": diffs}
        with open(os.path.join(self.data_dir, "compare", f"{datetime.now():%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def on_prod(self, _event, data, _kwargs):
        self._side(str(data.get("id") or ""), "prod", dict(data))

    def _side(self, mid: str, side: str, rec: dict):
        e = self.pending.setdefault(mid, {})
        e[side] = rec
        if "shadow" in e and "prod" in e:
            self._compare(mid, self.pending.pop(mid))
        else:
            self.run_in(lambda _: self._timeout(mid), 600)

    def _timeout(self, mid: str):
        e = self.pending.pop(mid, None)
        if e:
            self._compare(mid, e)

    def _compare(self, mid: str, e: dict):
        s, p = e.get("shadow"), e.get("prod")
        diffs = []
        if not s or not p:
            diffs.append("only_" + ("prod" if p else "shadow"))
        else:
            for k in ("kind", "weekly", "start", "end", "ndays"):
                if str(s.get(k)) != str(p.get(k)):
                    diffs.append(f"{k}: shadow={s.get(k)} prod={p.get(k)}")
            if bool(s.get("media")) != bool(p.get("local")):
                diffs.append(f"media: shadow={s.get('media')} prod={p.get('local')}")
            if bool(s.get("ftext_len")) != bool(int(p.get("ftext_len") or 0)):
                diffs.append(f"ftext: shadow={s.get('ftext_len')} prod={p.get('ftext_len')}")
            if s.get("error"):
                diffs.append("shadow_error: " + s["error"])
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "part": "ingest", "id": mid[-24:],
                "ok": not diffs, "diffs": diffs}
        with open(os.path.join(self.data_dir, "compare", f"{datetime.now():%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


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
