"""AppDaemon app: WhatsApp ingest (webhook -> filter -> media -> model -> queue -> alerts).

Live mode (shadow: false): this IS the pipeline. For every accepted message it
  1. downloads the attachment (pdf / image / audio / video) under /media/<media_root>/,
  2. reads documents and images-with-captions, transcribes voice messages (one model call),
  3. stores weekly schedules (input_text pointer + event weekly_plan_update + Telegram note),
  4. appends the message to the JSONL queue consumed by wa_summary,
  5. by day (06-23): after 30-120 s sends the WhatsApp read receipt and, for urgent /
     sign-up / emergency / name mentions, a Telegram alert. By night nothing: WaNight does it.
Shadow mode (shadow: true): same work, but only writes its own store and compares with
production events (wa_prod_ingest / wa_prod_alert). Nothing is sent or marked read.
"""
from __future__ import annotations

import importlib
import json
import os
import random
import shutil
import sys
import time
import urllib.request
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_store  # noqa: E402
import wa_send  # noqa: E402


class WaIngest(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        self.shadow = bool(c.get("shadow", True))
        wa_core.set_locale(c.get("language", "he"))
        self.data_dir = c["data_dir"]
        os.makedirs(os.path.join(self.data_dir, "compare"), exist_ok=True)
        self.queue = wa_store.JsonlQueue(os.path.join(self.data_dir, "queue.jsonl"))
        pdir = os.path.join(HERE, "prompts")
        self.prompts = {k: wa_core.load_prompt(pdir, k, "file_read") for k in ("file_read", "file_read_retry")}
        self.prompts["audio"] = wa_core.load_prompt(pdir, "audio", "audio")
        self.pending: dict[str, dict] = {}
        self.alerts: dict[str, dict] = {}
        self.listen_event(self.on_webhook, c.get("webhook_event", "wa_shadow_webhook"))
        self.listen_event(self.on_prod, "wa_prod_ingest")
        self.listen_event(self.on_prod_alert, "wa_prod_alert")
        self.run_daily(self.cleanup_media, c.get("cleanup_time", "04:10:00"))
        self.log(f"WaIngest ready (shadow={self.shadow})")

    # ------------------------------------------------------------ webhook
    def on_webhook(self, _event, data, _kwargs):
        raw = data.get("json") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        ev, p = raw.get("event", ""), raw.get("payload") or {}
        monitored = wa_core.monitored_groups(self.cfg["groups"], self.get_state(self.cfg["groups_helper"]) or "")
        ok, _reason = wa_core.accept_message(ev, p, monitored)
        if not ok:
            return
        mid, chat = str(p.get("id") or ""), str(p.get("from") or "")
        body = str(p.get("body") or "")
        sender, item = wa_core.queue_item_text(p)
        mclass = wa_core.media_class(p)
        rec = {"id": mid, "chat": chat, "kind": wa_core.message_kind(body, self.cfg.get("alert_names", [])),
               "media": None, "ftext_len": 0, "weekly": False, "start": "", "end": "", "ndays": 0, "error": ""}
        ftext, mime = "", str((p.get("media") or {}).get("mimetype") or "").split(";")[0].strip()
        try:
            rel = wa_core.media_rel_path(p, root=self.cfg["media_root"])
            if rel:
                self._download(p["media"]["url"], rel)
                rec["media"] = rel
                if wa_core.should_read(mclass, body, chat, self.cfg.get("staff_groups", [])):
                    if mclass == "audio":
                        ftext = self._transcribe(rel, mime)
                    else:
                        fr = self._read_file(rel, mime)
                        ftext = fr.get("text", "")
                        rec.update(weekly=fr["weekly"], start=fr["start"], end=fr["end"], ndays=len(fr["days"]))
                        if fr["weekly"]:
                            self._store_plan(chat, rel, fr, p)
                rec["ftext_len"] = len(ftext)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
            self.log(f"ingest media error {mid}: {rec['error']}", level="WARNING")
        daytime = wa_core.is_daytime(datetime.now())
        label = self.cfg.get("alert_group_labels", {}).get(chat.split("@")[0], chat.split("@")[0])
        alert = wa_core.day_alert(rec["kind"], label, sender, body) if daytime else None
        self.queue.append({"id": mid, "ts": p.get("timestamp") or 0, "chat": chat, "sender": sender, "item": item,
                           "caption": body if mclass != "none" else "", "ftext": ftext, "media": rec["media"], "mime": mime,
                           "mclass": mclass, "kind": rec["kind"], "alerted": bool(alert)})
        if self.shadow:
            if alert:
                self.alerts[mid] = alert
                self.run_in(lambda _: self._alert_timeout(mid), 600)
            self._side(mid, "shadow", rec)
        elif daytime:
            self.run_in(lambda _: self._seen_and_alert(chat, alert), random.randint(30, 120))

    # ------------------------------------------------------------ live actions
    def _seen_and_alert(self, chat: str, alert: dict | None):
        try:
            self.send_seen(chat)
        except Exception as e:  # noqa: BLE001
            self.log(f"sendSeen failed for {chat[-8:]}: {e}", level="WARNING")
        if alert:
            self.call_service("notify/send_message", entity_id=self.cfg["notify_entity"],
                              message=wa_core.telegram_safe(f"{alert['title']}\n{alert['text']}"))
            if self.cfg.get("alerts_to_whatsapp"):  # the family contact gets urgent alerts too
                wa_send.whatsapp_send(self.cfg, "text", text=f"{alert['title']}\n{alert['text']}", log=self.log)

    def send_seen(self, chat: str):
        wa_send.send_seen(self.cfg, chat)

    def _download(self, url: str, rel: str):
        url = url.replace("http://localhost:3000", self.cfg["waha_url"].rstrip("/"))
        dest = os.path.join("/media", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers=wa_send.waha_headers(self.cfg))
                with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return
            except Exception:  # noqa: BLE001
                if attempt:
                    raise
                time.sleep(5)

    # ------------------------------------------------------------ model
    def _ai(self, task: str, instructions: str, structure: dict, rel: str, mime: str):
        res = self.call_service(
            "ai_task/generate_data", return_response=True, hass_timeout=180,
            service_data={"entity_id": self.cfg["ai_task_entity"], "task_name": task, "instructions": instructions,
                          "structure": structure,
                          "attachments": [{"media_content_id": f"media-source://media_source/local/{rel}",
                                           "media_content_type": mime}]})
        return _find_key(res, "data")

    def _read_file(self, rel: str, mime: str) -> dict:
        """One Gemini call: text + weekly detection + days. Retry with the paraphrase prompt on failure."""
        empty = {"text": "", "weekly": False, "start": "", "end": "", "days": []}
        if not wa_core.safe_path(rel):
            return empty
        today = datetime.now().strftime("%Y-%m-%d")
        vals = {"TODAY": today, "OUTPUT_LANGUAGE": self.cfg.get("output_language", "Hebrew")}
        data = None
        for key in ("file_read", "file_read_retry"):
            pr = self.prompts[key]
            data = self._ai(f"file_{key}", wa_core.fill(pr["instructions"], vals), pr["structure"], rel, mime)
            if isinstance(data, dict) and data.get("text"):
                break
        data = data if isinstance(data, dict) else {}
        days = []
        for d in sorted([x for x in data.get("days") or [] if x.get("date")], key=lambda x: x["date"]):
            lessons = d.get("lessons") or []
            days.append({"date": d["date"], "weekday": d.get("weekday", ""), "no_school": bool(d.get("no_school")) and not lessons,
                         "hours": d.get("hours", ""), "lessons": lessons, "bring": wa_core.dedupe(d.get("bring") or []),
                         "notes": wa_core.dedupe(d.get("notes") or [])})
        return {"text": str(data.get("text") or "")[:6000], "weekly": bool(data.get("weekly_schedule")) and bool(days),
                "start": data.get("start") or "", "end": data.get("end") or "", "days": days}

    def _transcribe(self, rel: str, mime: str) -> str:
        if not wa_core.safe_path(rel):
            return ""
        pr = self.prompts["audio"]
        data = self._ai("audio_transcribe", wa_core.fill(pr["instructions"], {"OUTPUT_LANGUAGE": self.cfg.get("output_language", "Hebrew")}),
                        pr["structure"], rel, mime or "audio/ogg")
        return str((data or {}).get("text") or "")[:4000] if isinstance(data, dict) else ""

    # ------------------------------------------------------------ weekly plan
    def _store_plan(self, chat, rel, fr, payload):
        child = wa_core.child_for_chat(self.cfg["groups"], chat)
        today = datetime.now().strftime("%Y-%m-%d")
        if not child or not fr["end"] or fr["end"] < today:
            return
        path = os.path.join(self.data_dir, "weekly_plans.json")
        plans = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                plans = json.load(f)
        cur = plans.get(child)
        if cur and fr["start"] < cur.get("start", ""):
            return
        plans[child] = {"file": rel, "start": fr["start"], "end": fr["end"], "days": fr["days"]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=1)
        if self.shadow:
            return
        title = str((payload.get("media") or {}).get("filename") or payload.get("body") or "")
        pointer = self.cfg.get("plan_pointers", {}).get(child)
        if pointer:
            self.call_service("input_text/set_value", entity_id=pointer, value=f"{rel}|{fr['start']}|{fr['end']}|{title}"[:250])
        self.fire_event("weekly_plan_update", child=child, start=fr["start"], end=fr["end"], title=title, file=rel, days=fr["days"])
        name = self.cfg.get("child_names", {}).get(child, child)
        self.call_service("notify/send_message", entity_id=self.cfg["notify_entity"],
                          message=wa_core.telegram_safe(f"📅 נשמרה מערכת שבועית ל{name}: {fr['start']} – {fr['end']}."))

    # ------------------------------------------------------------ retention
    def cleanup_media(self, _kwargs):
        """Videos are kept a week, everything else keep_days (default three weeks)."""
        root = os.path.join("/media", self.cfg["media_root"])
        now = time.time()
        keep = {"mp4": 7, "mov": 7, "3gp": 7, "webm": 7}
        removed = 0
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                days = keep.get(fn.rsplit(".", 1)[-1].lower(), int(self.cfg.get("keep_days", 21)))
                p = os.path.join(dirpath, fn)
                if now - os.path.getmtime(p) > days * 86400:
                    os.remove(p)
                    removed += 1
        if removed:
            self.log(f"media cleanup: removed {removed} files")

    # ------------------------------------------------------------ shadow comparison
    def on_prod_alert(self, _event, data, _kwargs):
        if not self.shadow:
            return
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
        if self.shadow:
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
        self._log_compare("ingest", mid, diffs)


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
