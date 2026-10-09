"""AppDaemon app: evening / morning summary of the queued group messages.

Shadow mode: consumes its own queue (written by wa_ingest), calls Gemini with the same prompt
as production, builds the same message, but only stores the result and compares it with what
production sent (event wa_prod_summary). Nothing is sent, added to todo lists or calendars.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_store  # noqa: E402
import wa_send  # noqa: E402


EMPTY_OUT = {"items": [], "forms": [], "events": [], "msg": ""}


class WaSummary(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        self.shadow = bool(c.get("shadow", True))
        wa_core.set_locale(c.get("language", "he"))
        self.data_dir = c["data_dir"]
        os.makedirs(os.path.join(self.data_dir, "summary"), exist_ok=True)
        self.queue = wa_store.JsonlQueue(os.path.join(self.data_dir, "queue.jsonl"))
        pdir = os.path.join(HERE, "prompts")
        self.tpl = wa_core.load_prompt(pdir, "summary", "summary")
        self.tpl["files_note"] = wa_core.load_prompt(pdir, "summary_files_note")["instructions"]
        self.tpl["facts_rules"] = wa_core.load_prompt(pdir, "facts_rules")["instructions"]
        self.facts = wa_store.JsonFile(os.path.join(self.data_dir, "facts.json"), {"facts": []})
        for slot, t in c.get("times", {"evening": "21:00:00", "morning": "07:30:00"}).items():
            self.run_daily(self.run_summary, t, slot=slot)
        self.listen_event(self.on_prod, "wa_prod_summary")
        self.listen_event(lambda *_a, **_k: self.run_summary({"slot": "manual"}), "wa_shadow_summary_now")
        self.last: dict | None = None
        self.log(f"WaSummary ready (shadow={self.shadow})")

    # ------------------------------------------------------------ run
    def run_summary(self, kwargs):
        slot = kwargs.get("slot", "manual")
        raw_inbox = self.queue.read()
        morning_live = slot == "morning" and not self.shadow
        if not raw_inbox:
            if morning_live:  # nothing new, but the morning message still lists what is due today
                self._deliver(slot, EMPTY_OUT, [])
            return
        inbox = wa_core.collapse_albums(raw_inbox)  # unread photo/video runs -> one line, no model cost
        c, now = self.cfg, datetime.now()
        today = now.strftime("%Y-%m-%d")
        existing = self._existing_tasks()
        names = c["child_names"]
        vals = {
            "FAMILY_INTRO": c["family_intro"],
            "TODAY_DMY": now.strftime("%d.%m.%Y"),
            "TODAY_WD": now.strftime("%A"),
            "OUTPUT_LANGUAGE": c.get("output_language", "Hebrew"),
            "TOMORROW": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            "GROUP_MAP": "".join(f"- {gid}: {label}\n" for gid, label in c["group_labels"].items()),
            "INBOX": "".join(self._inbox_line(q) for q in inbox),
            "EXISTING": "".join(f"- {e.get('summary')} ({e.get('due')})\n" for e in existing),
            "SCHOOL_CHILDREN": ", ".join(c.get("school_children", [])),
            "NAMES_MENTION": c["names_mention"],
            "CHILD_OPTIONS": "|".join(names + ["כולם"]),
        }
        vals = {"FACTS_RULES": wa_core.fill(self.tpl["facts_rules"], {
            "CHILD_OPTIONS": vals["CHILD_OPTIONS"],
            "FACT_KEYS": wa_core.fact_keys_for_prompt(self.facts.read()["facts"]) or "(none yet)\n"}), **vals}
        attach = [q for q in inbox if q.get("media") and q.get("mclass") in ("pdf", "image") and not q.get("ftext")
                  and not q.get("_album") and wa_core.safe_path(q["media"])]
        if attach:
            vals["FILES_NOTE"] = wa_core.fill(self.tpl["files_note"], {
                "N": str(len(attach)),
                "FILE_MAP": "".join(f"\"{q['media'].split('/')[-1]}\" ← {c['group_labels'].get(q['chat'].split('@')[0], '')}; " for q in attach)})
        else:
            vals["FILES_NOTE"] = ""
        structure = json.loads(json.dumps(self.tpl["structure"]).replace('["{CHILD_OPTIONS_LIST}"]', json.dumps(names + ["כולם"], ensure_ascii=False)))
        sd = {"entity_id": c["ai_task_entity"], "task_name": f"shadow_summary_{slot}",
              "instructions": wa_core.fill(self.tpl["instructions"], vals), "structure": structure}
        if attach:
            sd["attachments"] = [{"media_content_id": f"media-source://media_source/local/{q['media']}",
                                  "media_content_type": q.get("mime") or "application/pdf"} for q in attach[:10]]
        data = None
        for _attempt in range(2):
            res = self.call_service("ai_task/generate_data", return_response=True, hass_timeout=180, service_data=sd)
            data = _find_key(res, "data")
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                break
            sd.pop("attachments", None)  # same fallback as production: retry without files
        if not (isinstance(data, dict) and isinstance(data.get("tasks"), list)):
            if not self.shadow:  # never fail silently: say so, keep the queue for the next run
                self.call_service("notify/send_message", entity_id=c["notify_entity"], message=wa_core.telegram_safe(wa_core.T("summary_failed", n=len(raw_inbox))))
                if morning_live:
                    self._deliver(slot, EMPTY_OUT, [])
            return self._record(slot, {"error": "gemini_failed", "inbox_n": len(inbox)})
        if isinstance(data.get("facts"), list) and data["facts"]:
            self.facts.update(lambda d: {"facts": wa_core.merge_facts(d.get("facts", []), data["facts"], today, source="summary")})
        src = " ".join(q["item"] + " " + (q.get("ftext") or "") for q in inbox)
        items = [t for t in data["tasks"] if t.get("action")]
        forms = wa_core.valid_forms(data.get("forms"), src)
        events = wa_core.parent_events(data.get("events"), today)
        msg = wa_core.summary_message(items, forms, events, len(inbox), today)
        out = {"slot": slot, "inbox_n": len(inbox), "items": items, "forms": forms, "events": events, "msg": msg,
               "todo_adds": [dict(zip(("due", "estimated"), wa_core.task_due(i, today), strict=True), item=f"{i['child']} — {i['action']}") for i in items]}
        self._record(slot, out)
        if not self.shadow:
            self._deliver(slot, out, raw_inbox)
        self.queue.drop({q["id"] for q in raw_inbox})

    def _inbox_line(self, q: dict) -> str:
        ts = datetime.fromtimestamp(q["ts"]).strftime("%d.%m %H:%M") if q.get("ts") else ""
        line = f"[{ts}] {q['item']}\n"
        if q.get("ftext"):
            label = "תמלול ההודעה הקולית" if q.get("mclass") == "audio" else "תוכן הקובץ המצורף"
            line += f"   ↳ {label}: {q['ftext'][:3000]}\n"
        return line

    def _no_school(self, now: datetime) -> bool:
        """Saturday, holiday (issur melacha) or school vacation: only explicitly dated items are listed."""
        c = self.cfg
        shabbat_sensor, vacation_sensor = c.get("issur_melacha_sensor"), c.get("school_calendar_sensor")
        return (now.strftime("%w") == "6" or (bool(shabbat_sensor) and self.get_state(shabbat_sensor) == "on")
                or (bool(vacation_sensor) and bool(self.get_state(vacation_sensor, attribute="elementary_vacation"))))

    def _existing_tasks(self) -> list[dict]:
        res = self.call_service("todo/get_items", return_response=True, service_data={
            "entity_id": self.cfg["tasks_todo"], "status": "needs_action"})
        items = _find_key(res, "items")
        return items if isinstance(items, list) else []

    # ------------------------------------------------------------ live delivery
    def _deliver(self, slot: str, out: dict, inbox: list[dict]):
        c, today = self.cfg, datetime.now().strftime("%Y-%m-%d")
        title = c.get("titles", {}).get(slot, c.get("title", "📚 סיכום מהקבוצות של הילדים"))
        items, forms, events, msg = out["items"], out["forms"], out["events"], out["msg"]
        # morning: ONE message = what is due today + what arrived overnight. The today list is read
        # BEFORE the new tasks are added, so nothing shows twice.
        new_msg = msg if (items or forms or events) else ""
        if slot == "morning":
            today_list = [i["summary"] for i in wa_core.morning_items(self._existing_tasks(), today, self._no_school(datetime.now()))]
            text = wa_core.combined_morning(today_list, new_msg)
        else:
            text = new_msg
        if text:
            self.call_service("notify/send_message", entity_id=c["notify_entity"], message=wa_core.telegram_safe(f"{title}\n{text}"))
            self._whatsapp("text", text=f"{title}\n\n{text}")
        # documents: every PDF, and every image the model could read as a document
        for q in inbox:
            if not q.get("media") or not wa_core.safe_path(q["media"]):
                continue
            is_doc = q.get("mclass") == "pdf" or (q.get("mclass") == "image" and q.get("ftext") and not q["ftext"].startswith("[תמונה]"))
            if not is_doc:
                continue
            name = (q.get("caption") or q["media"].split("/")[-1])[:80]
            group = c["group_labels"].get(q["chat"].split("@")[0], "")
            if c.get("telegram_config_entry"):
                self.call_service("telegram_bot/send_document", config_entry_id=c["telegram_config_entry"],
                                  file=f"/media/{q['media']}", caption=f"📎 {name}\n{group}")
            self._whatsapp("file", path=f"/media/{q['media']}", caption=f"📎 {name}\n{group}")
        # tasks
        for it in items:
            due, est = wa_core.task_due(it, today)
            desc = str(it.get("source") or "") + (" · " + wa_core.estimated_mark() if est else "")
            self.call_service("todo/add_item", entity_id=c["tasks_todo"], item=f"{it['child']} — {it['action']}", due_date=due, description=desc)
        # forms -> tasks with the link (no duplicates by url)
        existing = self._existing_tasks()
        for f in forms:
            if any(f["url"] in str(e.get("description") or "") for e in existing):
                continue
            due = f["due"] or (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
            self.call_service("todo/add_item", entity_id=c["tasks_todo"], item=f"{f['child']} — 📝 למלא: {f['title']}", due_date=due, description=f["url"])
        # parent events -> calendars (no duplicates by normalised title)
        cals = [x.strip() for x in str(self.get_state(c["calendars_helper"]) or "").split(",") if x.strip().startswith("calendar.")]
        for e in events:
            for cal in cals:
                if self._event_exists(cal, e):
                    continue
                data = {"summary": f"👨‍👩‍👧 {e['child']}: {e['title']}", "location": e.get("location") or "",
                        "description": "נוסף אוטומטית מקבוצות הוואטסאפ.\n" + str(e.get("evidence") or "")}
                if e.get("start"):
                    start = datetime.strptime(f"{e['date']} {e['start']}", "%Y-%m-%d %H:%M")
                    end = datetime.strptime(f"{e['date']} {e['end']}", "%Y-%m-%d %H:%M") if e.get("end") else start + timedelta(hours=1)
                    data.update(start_date_time=start.strftime("%Y-%m-%d %H:%M:00"), end_date_time=end.strftime("%Y-%m-%d %H:%M:00"))
                else:
                    nxt = (datetime.strptime(e["date"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                    data.update(start_date=e["date"], end_date=nxt)
                self.call_service("calendar/create_event", entity_id=cal, **data)
        if c.get("last_summary_helper"):
            self.call_service("input_text/set_value", entity_id=c["last_summary_helper"],
                              value=f"{datetime.now():%d.%m %H:%M} · {len(items)} משימות, {len(inbox)} הודעות"[:250])

    def _event_exists(self, cal: str, e: dict) -> bool:
        res = self.call_service("calendar/get_events", return_response=True, service_data={
            "entity_id": cal, "start_date_time": f"{e['date']} 00:00:00", "end_date_time": f"{e['date']} 23:59:59"})
        evs = _find_key(res, "events") or []
        k = wa_core.norm(e["title"])[:12]
        return bool(k) and any(k in wa_core.norm(x.get("summary", "")) for x in evs)

    def _whatsapp(self, mode: str, text: str = "", path: str = "", caption: str = ""):
        wa_send.whatsapp_send(self.cfg, mode, text=text, path=path, caption=caption, log=self.log)

    # ------------------------------------------------------------ compare
    def _record(self, slot, out):
        self.last = {"ts": datetime.now().isoformat(timespec="seconds"), **out}
        with open(os.path.join(self.data_dir, "summary", f"{datetime.now():%Y-%m-%d}_{slot}.json"), "w", encoding="utf-8") as f:
            json.dump(self.last, f, ensure_ascii=False, indent=1)

    def on_prod(self, _event, data, _kwargs):
        prod = {k: data.get(k) for k in ("items", "forms", "events", "msg", "inbox_n", "title")}
        for k in ("items", "forms", "events"):
            if isinstance(prod[k], str):
                prod[k] = json.loads(prod[k] or "[]")
        self.run_in(lambda _: self._compare(prod), 240)  # the shadow run may take two model calls

    def _compare(self, prod: dict):
        s = self.last or {}
        diffs = []
        if not s or s.get("error"):
            diffs.append("shadow_missing_or_failed: " + str(s.get("error", "none")))
        else:
            key = lambda i: (i.get("child"), i.get("date") or "")  # noqa: E731
            sp, pp = sorted(map(key, s["items"])), sorted(map(key, prod.get("items") or []))
            if len(sp) != len(pp):
                diffs.append(f"items: shadow={len(sp)} prod={len(pp)}")
            if set(sp) != set(pp):
                diffs.append(f"child/date only_shadow={sorted(set(sp) - set(pp))} only_prod={sorted(set(pp) - set(sp))}")
            su, pu = {f["url"] for f in s["forms"]}, {f.get("url") for f in prod.get("forms") or []}
            if su != pu:
                diffs.append(f"forms: shadow={sorted(su)} prod={sorted(pu)}")
            se, pe = {e["date"] for e in s["events"]}, {e.get("date") for e in prod.get("events") or []}
            if se != pe:
                diffs.append(f"parent_events dates: shadow={sorted(se)} prod={sorted(pe)}")
        line = {"ts": datetime.now().isoformat(timespec="seconds"), "part": "summary", "ok": not diffs, "diffs": diffs,
                "shadow_items": len(s.get("items") or []), "prod_items": len(prod.get("items") or [])}
        with open(os.path.join(self.data_dir, "compare", f"{datetime.now():%Y-%m-%d}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        self.last = None


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
