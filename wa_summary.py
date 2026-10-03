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


class WaSummary(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        self.shadow = bool(c.get("shadow", True))
        self.data_dir = c["data_dir"]
        os.makedirs(os.path.join(self.data_dir, "summary"), exist_ok=True)
        pdir = os.path.join(HERE, "prompts")
        self.tpl = wa_core.load_prompt(pdir, "summary", "summary")
        self.tpl["files_note"] = wa_core.load_prompt(pdir, "summary_files_note")["instructions"]
        for slot, t in c.get("times", {"evening": "21:00:00", "morning": "07:30:00"}).items():
            self.run_daily(self.run_summary, t, slot=slot)
        self.listen_event(self.on_prod, "wa_prod_summary")
        self.listen_event(lambda *_a, **_k: self.run_summary({"slot": "manual"}), "wa_shadow_summary_now")
        self.last: dict | None = None
        self.log(f"WaSummary ready (shadow={self.shadow})")

    # ------------------------------------------------------------ queue (shared format with wa_ingest)
    def _queue_path(self):
        return os.path.join(self.data_dir, "queue.jsonl")

    def _read_queue(self) -> list[dict]:
        p = self._queue_path()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def _drop_queue(self, ids: set[str]):
        rest = [q for q in self._read_queue() if q["id"] not in ids]
        with open(self._queue_path(), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(q, ensure_ascii=False) + "\n" for q in rest)

    # ------------------------------------------------------------ run
    def run_summary(self, kwargs):
        slot = kwargs.get("slot", "manual")
        inbox = self._read_queue()
        if not inbox:
            return
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
        attach = [q for q in inbox if q.get("media") and not q.get("ftext") and wa_core.safe_path(q["media"])]
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
        for attempt in range(2):
            res = self.call_service("ai_task/generate_data", return_response=True, hass_timeout=180, service_data=sd)
            data = _find_key(res, "data")
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                break
            sd.pop("attachments", None)  # same fallback as production: retry without files
        if not (isinstance(data, dict) and isinstance(data.get("tasks"), list)):
            return self._record(slot, {"error": "gemini_failed", "inbox_n": len(inbox)})
        src = " ".join(q["item"] + " " + (q.get("ftext") or "") for q in inbox)
        items = [t for t in data["tasks"] if t.get("action")]
        forms = wa_core.valid_forms(data.get("forms"), src)
        events = wa_core.parent_events(data.get("events"), today)
        msg = wa_core.summary_message(items, forms, events, len(inbox), today)
        out = {"slot": slot, "inbox_n": len(inbox), "items": items, "forms": forms, "events": events, "msg": msg,
               "todo_adds": [dict(zip(("due", "estimated"), wa_core.task_due(i, today)), item=f"{i['child']} — {i['action']}") for i in items]}
        self._record(slot, out)
        self._drop_queue({q["id"] for q in inbox})
        # non-shadow (later): notify, send PDFs, todo.add_item, calendar.create_event

    def _inbox_line(self, q: dict) -> str:
        ts = datetime.fromtimestamp(q["ts"]).strftime("%d.%m %H:%M") if q.get("ts") else ""
        line = f"[{ts}] {q['item']}\n"
        if q.get("ftext"):
            line += f"   ↳ תוכן הקובץ המצורף: {q['ftext'][:3000]}\n"
        return line

    def _existing_tasks(self) -> list[dict]:
        res = self.call_service("todo/get_items", return_response=True, service_data={
            "entity_id": self.cfg["tasks_todo"], "status": "needs_action"})
        items = _find_key(res, "items")
        return items if isinstance(items, list) else []

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
        self.run_in(lambda _: self._compare(prod), 120)  # give the shadow run time to finish

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
