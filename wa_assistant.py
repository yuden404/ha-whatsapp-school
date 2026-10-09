"""AppDaemon app: the family assistant on the WhatsApp number (see docs/assistant-plan.md).

Only the allowed contacts are answered. One model call per message returns a route:
  school / smalltalk -> the answer, built only from the context packet (facts, schedules, tasks,
                        calendar, recent group messages),
  remember           -> the fact is stored in the shared facts memory, then confirmed,
  home               -> the same Home Assistant conversation agent as the Telegram bot answers.
The model only produces text; every side effect is a code path gated by the route and the allowlist.
Rollout: only contacts in reply_contacts get answers; questions from the others are logged only.
"""
from __future__ import annotations

import importlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_send  # noqa: E402
import wa_store  # noqa: E402

LID_IN_ID = re.compile(r"(\d{8,})@lid")
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class WaAssistant(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        wa_core.set_locale(c.get("language", "he"))
        self.data_dir = c["data_dir"]
        self.allowed = list(c.get("allowed_contacts", []))
        self.reply_to = set(c.get("reply_contacts", []))
        self.names = c.get("contact_names", {})
        self.tpl = wa_core.load_prompt(os.path.join(HERE, "prompts"), "assistant", "assistant")
        self.facts = wa_store.JsonFile(os.path.join(self.data_dir, "facts.json"), {"facts": []})
        self.lids = wa_store.JsonFile(os.path.join(self.data_dir, "lids.json"), {})
        self.history: dict[str, deque] = {}
        self.rate: dict[str, deque] = {}
        self.lock = threading.Lock()
        self.listen_event(self.on_webhook, c.get("webhook_event", "wa_shadow_webhook"))
        self.run_in(lambda _: self.resolve_lids(), int(c.get("lid_delay", 20)))

    # ------------------------------------------------------------ identity
    def _get(self, path: str):
        req = urllib.request.Request(self.cfg["waha_url"].rstrip("/") + path, headers=wa_send.waha_headers(self.cfg))
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def resolve_lids(self):
        """WhatsApp may show a private sender as '<n>@lid'. Message ids in the allowed chats carry that LID."""
        found = {}
        for chat in self.allowed:
            try:
                msgs = self._get(f"/api/default/chats/{chat}/messages?" + urllib.parse.urlencode({"limit": 30, "downloadMedia": "false"}))
            except Exception as e:  # noqa: BLE001
                self.log(f"lid resolve {chat[-6:]} failed: {e}", level="WARNING")
                continue
            for m in msgs:
                if m.get("fromMe"):
                    continue
                for lid in LID_IN_ID.findall(str(m.get("id") or "") + " " + str(m.get("from") or "")):
                    found[f"{lid}@lid"] = chat
        if found:
            self.lids.update(lambda d: {**d, **found})
        return found

    def contact_of(self, sender: str) -> str | None:
        if sender in self.allowed:
            return sender
        if sender.endswith("@lid"):
            known = self.lids.read().get(sender)
            if known:
                return known
            return self.resolve_lids().get(sender)
        return None

    # ------------------------------------------------------------ events
    def on_webhook(self, _event, data, _kwargs):
        raw = data.get("json") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        if raw.get("event") != "message":
            return
        p = raw.get("payload") or {}
        sender = str(p.get("from") or "")
        if p.get("fromMe") or sender.endswith("@g.us") or sender.endswith("@newsletter") or sender == "status@broadcast":
            return
        media = p.get("media") or {}
        mime = str(media.get("mimetype") or "")
        if p.get("hasMedia") and not mime.startswith("audio/"):
            return  # files (chat exports) are handled by wa_private
        contact = self.contact_of(sender)
        if not contact:
            return
        if not self._allow_rate(contact):
            if self._first_over_limit(contact):
                self._send(contact, wa_core.T("assistant_busy"))
            return
        self.run_in(lambda _: self.handle(contact, p), random.randint(2, 6))

    def _allow_rate(self, contact: str) -> bool:
        q = self.rate.setdefault(contact, deque())
        now = time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= int(self.cfg.get("rate_limit_per_hour", 20)):
            return False
        q.append(now)
        return True

    def _first_over_limit(self, contact: str) -> bool:
        key = f"_busy_{contact}"
        last = getattr(self, key, 0)
        setattr(self, key, time.time())
        return time.time() - last > 3600

    # ------------------------------------------------------------ answer
    def handle(self, contact: str, p: dict):
        with self.lock:
            question = str(p.get("body") or "").strip()
            if p.get("hasMedia"):
                question = self.transcribe(p) or question
            if not question:
                return
            try:
                data = self.ask(contact, question)
            except Exception as e:  # noqa: BLE001
                self.log(f"assistant failed: {e}", level="WARNING")
                data = None
            route = (data or {}).get("route") or "smalltalk"
            answer = str((data or {}).get("answer") or "").strip()
            if data is None:
                answer = wa_core.T("assistant_failed")
            elif route == "remember":
                answer = self.remember(contact, data.get("fact") or {}) or answer
            elif route == "home":
                answer = self.home(contact, question)
            if not answer:
                answer = wa_core.T("assistant_failed")
            self._send(contact, answer)
            self._log(contact, question, route, answer, (data or {}).get("confidence", ""))
            hist = self.history.setdefault(contact, deque(maxlen=6))
            hist.append((question, answer))

    def context(self) -> str:
        c, now = self.cfg, datetime.now()
        today = now.strftime("%Y-%m-%d")
        plans = {}
        path = os.path.join(self.data_dir, "weekly_plans.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                plans = json.load(f)
        tasks = _find_key(self.call_service("todo/get_items", return_response=True, service_data={
            "entity_id": c["tasks_todo"], "status": "needs_action"}), "items") or []
        horizon = (now + timedelta(days=14)).strftime("%Y-%m-%d")
        tasks = [t for t in tasks if not t.get("due") or str(t.get("due")) <= horizon]
        events = []
        cal_list = str(self.get_state(c["calendars_helper"]) or "") if c.get("calendars_helper") else ""
        for cal in [x.strip() for x in cal_list.split(",") if x.strip().startswith("calendar.")]:
            res = self.call_service("calendar/get_events", return_response=True, service_data={
                "entity_id": cal, "start_date_time": now.strftime("%Y-%m-%d 00:00:00"),
                "end_date_time": (now + timedelta(days=30)).strftime("%Y-%m-%d 23:59:59")})
            events += _find_key(res, "events") or []
        since = time.time() - int(c.get("context_days", 14)) * 86400
        labels = c.get("group_labels", {})
        msgs = []
        for path in sorted({wa_store.archive_path(self.data_dir, since), wa_store.archive_path(self.data_dir, time.time())}):
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if (r.get("ts") or 0) < since:
                        continue
                    gid = r["chat"].split("@")[0]
                    text = wa_core.text_of_item(r.get("item", ""))
                    if r.get("ftext"):
                        text += " ↳ " + r["ftext"][:1500]
                    ts = datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M")
                    msgs.append(f"[{ts}] {labels.get(gid, gid)} | {r.get('sender', '')}: {text}\n")
        names = c.get("child_names", {})
        return wa_core.build_context(self.facts.read()["facts"], plans, names, tasks, events, msgs, today, int(c.get("context_max_chars", 60000)))

    def ask(self, contact: str, question: str) -> dict | None:
        c, now = self.cfg, datetime.now()
        tmr = now + timedelta(days=1)
        names = list(c.get("child_names", {}).values())
        hist = "".join(f"- {self.names.get(contact, 'parent')}: {q}\n- you: {a}\n" for q, a in self.history.get(contact, [])) or "(none)\n"
        vals = {"WHO": self.names.get(contact, "a parent"), "FAMILY_INTRO": c.get("family_intro", ""),
                "TODAY": now.strftime("%Y-%m-%d"), "TODAY_WD": WEEKDAYS_EN[now.weekday()],
                "TOMORROW": tmr.strftime("%Y-%m-%d"), "TOMORROW_WD": WEEKDAYS_EN[tmr.weekday()],
                "GROUP_MAP": "".join(f"- {lbl}\n" for lbl in c.get("group_labels", {}).values()),
                "HISTORY": hist, "QUESTION": question, "OUTPUT_LANGUAGE": c.get("output_language", "Hebrew"),
                "CHILD_OPTIONS": "|".join(names + ["כולם"])}
        instructions = wa_core.fill(self.tpl["instructions"], {"CONTEXT": self.context(), **vals})
        structure = json.loads(json.dumps(self.tpl["structure"]).replace('["{CHILD_OPTIONS_LIST}"]', json.dumps(names + ["כולם"], ensure_ascii=False)))
        res = self.call_service("ai_task/generate_data", return_response=True, hass_timeout=120, service_data={
            "entity_id": c["ai_task_entity"], "task_name": "assistant", "instructions": instructions, "structure": structure})
        data = _find_key(res, "data")
        return data if isinstance(data, dict) else None

    def remember(self, contact: str, fact: dict) -> str:
        key, value = str(fact.get("key") or "").strip(), str(fact.get("value") or "").strip()
        if not key or not value:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        who = self.names.get(contact, contact.split("@")[0][-4:])
        self.facts.update(lambda d: {"facts": wa_core.merge_facts(d.get("facts", []), [fact], today, source=f"chat:{who}")})
        return wa_core.T("assistant_saved", key=key, value=value)

    def home(self, contact: str, text: str) -> str:
        agent = self.cfg.get("home_agent")
        if not agent:
            return wa_core.T("assistant_home_off")
        try:
            res = self.call_service("conversation/process", return_response=True, hass_timeout=60, service_data={
                "text": text, "language": self.cfg.get("language", "he"), "agent_id": agent,
                "conversation_id": f"whatsapp_{contact.split('@')[0]}"})
            speech = _find_key(res, "speech")
            plain = speech.get("plain", {}).get("speech") if isinstance(speech, dict) else None
            return str(plain or "") or wa_core.T("assistant_home_failed")
        except Exception as e:  # noqa: BLE001
            self.log(f"home agent failed: {e}", level="WARNING")
            return wa_core.T("assistant_home_failed")

    def transcribe(self, p: dict) -> str:
        try:
            url = str((p.get("media") or {}).get("url") or "").replace("http://localhost:3000", self.cfg["waha_url"].rstrip("/"))
            rel = f"{self.cfg.get('media_root', 'whatsapp')}/assistant/{int(time.time())}.ogg"
            dest = os.path.join("/media", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with urllib.request.urlopen(urllib.request.Request(url, headers=wa_send.waha_headers(self.cfg)), timeout=60) as r, open(dest, "wb") as f:
                f.write(r.read())
            pr = wa_core.load_prompt(os.path.join(HERE, "prompts"), "audio", "audio")
            res = self.call_service("ai_task/generate_data", return_response=True, hass_timeout=120, service_data={
                "entity_id": self.cfg["ai_task_entity"], "task_name": "assistant_voice",
                "instructions": wa_core.fill(pr["instructions"], {"OUTPUT_LANGUAGE": self.cfg.get("output_language", "Hebrew")}),
                "structure": pr["structure"],
                "attachments": [{"media_content_id": f"media-source://media_source/local/{rel}", "media_content_type": "audio/ogg"}]})
            data = _find_key(res, "data")
            return str((data or {}).get("text") or "") if isinstance(data, dict) else ""
        except Exception as e:  # noqa: BLE001
            self.log(f"voice transcription failed: {e}", level="WARNING")
            return ""

    # ------------------------------------------------------------ out
    def _send(self, contact: str, text: str):
        if contact in self.reply_to:
            wa_send.whatsapp_send(self.cfg, "text", text=text, log=self.log, to=contact)

    def _log(self, contact: str, q: str, route: str, a: str, conf: str):
        path = os.path.join(self.data_dir, "assistant", f"{datetime.now():%Y-%m}.jsonl")
        wa_store.JsonlQueue(path).append({"ts": int(time.time()), "who": self.names.get(contact, contact[-8:]), "q": q,
                                          "route": route, "answer": a, "confidence": conf, "replied": contact in self.reply_to})


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
