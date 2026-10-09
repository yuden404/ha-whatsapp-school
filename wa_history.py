"""AppDaemon app: one-off import of old group messages into the archive, then facts extraction.

Trigger: event wa_import_history {"since": "YYYY-MM-DD", "facts": true}. Idempotent: message ids
already in the archive are skipped. Text only, no media download (old media is usually gone).
Result: sensor.wa_history and one Telegram line.
"""
from __future__ import annotations

import glob
import importlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_send  # noqa: E402
import wa_store  # noqa: E402


class WaHistory(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        self.cfg = self.args
        wa_core.set_locale(self.cfg.get("language", "he"))
        self.data_dir = self.cfg["data_dir"]
        pdir = os.path.join(HERE, "prompts")
        self.tpl = wa_core.load_prompt(pdir, "facts", "facts")
        self.tpl["facts_rules"] = wa_core.load_prompt(pdir, "facts_rules")["instructions"]
        self.facts = wa_store.JsonFile(os.path.join(self.data_dir, "facts.json"), {"facts": []})
        self.listen_event(self.on_import, "wa_import_history")

    # ------------------------------------------------------------ WAHA
    def fetch(self, chat: str, since_ts: int) -> list[dict]:
        """All messages of one chat since since_ts (WAHA returns newest first, page by offset)."""
        out, offset, page = [], 0, int(self.cfg.get("page_size", 200))
        while True:
            q = urllib.parse.urlencode({"limit": page, "offset": offset, "downloadMedia": "false", "filter.timestamp.gte": since_ts})
            url = f"{self.cfg['waha_url'].rstrip('/')}/api/default/chats/{chat}/messages?{q}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=wa_send.waha_headers(self.cfg)), timeout=60) as r:
                batch = json.load(r)
            out += batch
            if len(batch) < page or offset > 20000:
                return out
            offset += page

    # ------------------------------------------------------------ import
    def on_import(self, _event, data, _kwargs):
        since = str(data.get("since") or "")
        since_ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp()) if since else int(datetime.now().timestamp()) - 90 * 86400
        known = set()
        for p in glob.glob(os.path.join(self.data_dir, "archive", "*.jsonl")):
            with open(p, encoding="utf-8") as f:
                known.update(json.loads(line)["id"] for line in f if line.strip())
        added, errors = [], []
        for gid in self.cfg["groups"]:
            chat = f"{gid}@g.us"
            try:
                msgs = self.fetch(chat, since_ts)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{gid[-4:]}: {type(e).__name__}")
                continue
            for p in msgs:
                ok, _ = wa_core.accept_message("message", p, [gid])
                mid = str(p.get("id") or "")
                if not ok or not mid or mid in known:
                    continue
                sender, item = wa_core.queue_item_text(p)
                rec = {"id": mid, "ts": p.get("timestamp") or 0, "chat": chat, "sender": sender, "item": item,
                       "caption": "", "ftext": "", "media": None, "mime": "", "mclass": wa_core.media_class(p),
                       "kind": wa_core.message_kind(p.get("body"), self.cfg.get("alert_names", [])), "alerted": True, "imported": True}
                wa_store.JsonlQueue(wa_store.archive_path(self.data_dir, rec["ts"] or None)).append(rec)
                known.add(mid)
                added.append(rec)
        n_facts = self.extract_facts(added) if data.get("facts", True) and added else 0
        result = {"since": since or "90d", "imported": len(added), "facts_added": n_facts, "errors": errors,
                  "ran_at": datetime.now().isoformat(timespec="seconds")}
        self.set_state("sensor.wa_history", state="done" if not errors else "partial", attributes=result, replace=True)
        self.call_service("notify/send_message", entity_id=self.cfg["notify_entity"], message=wa_core.telegram_safe(
            f"📚 ייבוא היסטוריה: {len(added)} הודעות, {n_facts} עובדות חדשות" + (f" (שגיאות: {', '.join(errors)})" if errors else "")))

    def extract_facts(self, records: list[dict]) -> int:
        return extract_facts(self, self.cfg, self.tpl, self.facts, records)


def extract_facts(app, c: dict, tpl: dict, facts, records: list[dict], source: str = "history") -> int:
    """Chronological chunks of text records -> facts (later chunks win, merge_facts supersedes). Returns new rows."""
    today = datetime.now().strftime("%Y-%m-%d")
    names = list(c["child_names"].values()) if isinstance(c.get("child_names"), dict) else list(c.get("child_names", []))
    before = len(facts.read()["facts"])
    lines = []
    for r in sorted(records, key=lambda r: r["ts"] or 0):
        ts = datetime.fromtimestamp(r["ts"]).strftime("%d.%m.%Y %H:%M") if r["ts"] else ""
        if r.get("mclass", "none") == "none":
            lines.append(f"[{ts}] {r['item']}\n")
    budget = int(c.get("chunk_chars", 40000))
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) > budget and cur:
            chunks.append(cur)
            cur = ""
        cur += ln
    if cur:
        chunks.append(cur)
    structure = json.loads(json.dumps(tpl["structure"]).replace('["{CHILD_OPTIONS_LIST}"]', json.dumps(names + ["כולם"], ensure_ascii=False)))
    for chunk in chunks:
        keys = wa_core.fact_keys_for_prompt(facts.read()["facts"]) or "(none yet)\n"
        rules = wa_core.fill(tpl["facts_rules"], {"CHILD_OPTIONS": "|".join(names + ["כולם"]), "FACT_KEYS": keys})
        instructions = wa_core.fill(tpl["instructions"], {
            "FAMILY_INTRO": c.get("family_intro", ""), "OUTPUT_LANGUAGE": c.get("output_language", "Hebrew"),
            "GROUP_MAP": "".join(f"- {g}: {lbl}\n" for g, lbl in c.get("group_labels", {}).items()),
            "INBOX": chunk, "FACTS_RULES": rules})
        res = app.call_service("ai_task/generate_data", return_response=True, hass_timeout=240, service_data={
            "entity_id": c["ai_task_entity"], "task_name": f"{source}_facts", "instructions": instructions, "structure": structure})
        data = _find_key(res, "data")
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            facts.update(lambda d, new=data["facts"]: {"facts": wa_core.merge_facts(d.get("facts", []), new, today, source=source)})
    return len(facts.read()["facts"]) - before


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
