"""AppDaemon app: private messages to the WAHA number from the two allowed contacts.

Step 1 of the assistant plan: a parent shares a WhatsApp "export chat" file (zip or txt) with the
number. The file is parsed, matched to a monitored group (by name, else by overlapping messages in
the archive), merged into the archive without duplicates, facts are extracted, and the sender gets
one confirmation. Messages from anyone else are ignored. Files sent while the app was down are picked
up by a scan of the allowed chats at start.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wa_core  # noqa: E402
import wa_history  # noqa: E402
import wa_send  # noqa: E402
import wa_store  # noqa: E402

EXPORT_MIMES = ("application/zip", "application/x-zip-compressed", "text/plain")


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        import pytz
        return pytz.timezone(name)


class WaPrivate(hass.Hass):
    def initialize(self):
        importlib.reload(wa_core)
        c = self.cfg = self.args
        wa_core.set_locale(c.get("language", "he"))
        self.data_dir = c["data_dir"]
        self.allowed = set(c.get("allowed_contacts", []))
        self.tz = _tz(c.get("time_zone", "Asia/Jerusalem"))
        pdir = os.path.join(HERE, "prompts")
        self.tpl = wa_core.load_prompt(pdir, "facts", "facts")
        self.tpl["facts_rules"] = wa_core.load_prompt(pdir, "facts_rules")["instructions"]
        self.facts = wa_store.JsonFile(os.path.join(self.data_dir, "facts.json"), {"facts": []})
        self.done = wa_store.JsonFile(os.path.join(self.data_dir, "imports.json"), {"processed": []})
        self.subjects: dict[str, str] = {}
        self.lock = threading.Lock()  # webhook and scan may run on different threads
        self.listen_event(self.on_webhook, c.get("webhook_event", "wa_shadow_webhook"))
        self.run_in(lambda _: self.startup(), int(c.get("scan_delay", 30)))

    def startup(self):
        self.subjects = self.group_subjects()
        self.scan()

    # ------------------------------------------------------------ WAHA
    def _get(self, path: str):
        req = urllib.request.Request(self.cfg["waha_url"].rstrip("/") + path, headers=wa_send.waha_headers(self.cfg))
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def group_subjects(self) -> dict[str, str]:
        out = {}
        for gid in self.cfg["groups"]:
            try:
                g = self._get(f"/api/default/groups/{gid}@g.us")
                out[gid] = str(g.get("subject") or g.get("name") or (g.get("groupMetadata") or {}).get("subject") or "")
            except Exception:  # noqa: BLE001
                out[gid] = ""
        return out

    def scan(self):
        """Exports sent while the app was not listening: last 100 messages of each allowed chat."""
        for chat in self.allowed:
            try:
                q = urllib.parse.urlencode({"limit": 100, "downloadMedia": "true"})
                msgs = self._get(f"/api/default/chats/{chat}/messages?{q}")
            except Exception as e:  # noqa: BLE001
                self.log(f"scan {chat[-6:]} failed: {e}", level="WARNING")
                continue
            for p in sorted(msgs, key=lambda m: m.get("timestamp") or 0):
                if not p.get("fromMe") and self.is_export(p):
                    self.process(p, reply_to=chat)

    # ------------------------------------------------------------ events
    def on_webhook(self, _event, data, _kwargs):
        raw = data.get("json") or {}
        if isinstance(raw, str):
            raw = json.loads(raw)
        if raw.get("event") != "message":
            return
        p = raw.get("payload") or {}
        chat = str(p.get("from") or "")
        if p.get("fromMe") or chat.endswith("@g.us") or not self.is_export(p):
            return
        if chat in self.allowed:
            self.process(p, reply_to=chat)
        elif chat.endswith("@lid"):
            # WhatsApp now often identifies private senders by a LID instead of the phone number. We cannot
            # check a LID against the allowlist, so we scan the allowed chats instead: a stranger finds nothing.
            self.run_in(lambda _: self.scan(), 5)

    @staticmethod
    def filename(p: dict) -> str:
        m = p.get("media") or {}
        return str(m.get("filename") or (p.get("_data") or {}).get("filename") or p.get("body") or "")

    def is_export(self, p: dict) -> bool:
        m = p.get("media") or {}
        mime = str(m.get("mimetype") or "").split(";")[0].strip()
        name = self.filename(p).lower()
        return bool(p.get("hasMedia")) and (mime in EXPORT_MIMES or name.endswith((".zip", ".txt")))

    # ------------------------------------------------------------ import
    def download(self, p: dict) -> bytes:
        url = str((p.get("media") or {}).get("url") or "").replace("http://localhost:3000", self.cfg["waha_url"].rstrip("/"))
        req = urllib.request.Request(url, headers=wa_send.waha_headers(self.cfg))
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    @staticmethod
    def export_text(blob: bytes) -> str:
        if blob[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                txt = [n for n in z.namelist() if n.lower().endswith(".txt")]
                if not txt:
                    return ""
                return z.read(sorted(txt, key=lambda n: (n != "_chat.txt", n))[0]).decode("utf-8", "replace")
        return blob.decode("utf-8", "replace")

    def archive_index(self) -> tuple[set, dict]:
        """Existing dedupe keys (with +/- 1 minute) and, per group, the set of normalised texts."""
        keys, texts = set(), {}
        adir = os.path.join(self.data_dir, "archive")
        for fn in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(adir, fn), encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    gid = r["chat"].split("@")[0]
                    t = wa_core.text_of_item(r.get("item", ""))
                    for d in (-60, 0, 60):
                        keys.add(wa_core.export_key(gid, int(r.get("ts") or 0) + d, t))
                    texts.setdefault(gid, set()).add(wa_core.norm(t)[:60])
        return keys, texts

    def process(self, p: dict, reply_to: str):
        with self.lock:
            self._process(p, reply_to)

    def _process(self, p: dict, reply_to: str):
        mid = str(p.get("id") or "")
        if mid and mid in self.done.read()["processed"]:
            return
        name = wa_core.export_group_name(self.filename(p)) or "?"
        try:
            text = self.export_text(self.download(p))
        except Exception as e:  # noqa: BLE001
            self.log(f"export download failed: {e}", level="WARNING")
            return  # not marked processed: the next scan retries
        rows = [r for r in wa_core.parse_whatsapp_export(text) if not r["media"]]
        if not rows:
            self._reply(reply_to, wa_core.T("import_bad", name=name))
            return self._mark(mid)
        keys, texts = self.archive_index()
        gid = wa_core.match_group_by_name(name, self.subjects) or wa_core.match_group_by_content([r["text"] for r in rows], texts)[0]
        if not gid:
            os.makedirs(os.path.join(self.data_dir, "import", "unmatched"), exist_ok=True)
            with open(os.path.join(self.data_dir, "import", "unmatched", f"{hashlib.sha1(mid.encode()).hexdigest()[:12]}.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            self._reply(reply_to, wa_core.T("import_unknown", name=name))
            return self._mark(mid)
        chat = f"{gid}@g.us"
        new, dup = [], 0
        for r in rows:
            ts = int(datetime(r["y"], r["mo"], r["d"], r["h"], r["mi"], r["s"], tzinfo=self.tz).timestamp()) \
                if hasattr(self.tz, "key") else int(self.tz.localize(datetime(r["y"], r["mo"], r["d"], r["h"], r["mi"], r["s"])).timestamp())
            k = wa_core.export_key(gid, ts, r["text"])
            if k in keys:
                dup += 1
                continue
            keys.add(k)
            text_ = r["text"] if len(r["text"]) <= 600 else r["text"][:599] + "…"
            rec = {"id": "exp_" + hashlib.sha1(k.encode()).hexdigest()[:16], "ts": ts, "chat": chat, "sender": r["sender"],
                   "item": f"{gid} | {r['sender']}: {text_}", "caption": "", "ftext": "", "media": None, "mime": "",
                   "mclass": "none", "kind": "none", "alerted": True, "imported": "export"}
            wa_store.JsonlQueue(wa_store.archive_path(self.data_dir, ts)).append(rec)
            new.append(rec)
        n_facts = wa_history.extract_facts(self, self.cfg, self.tpl, self.facts, new, source="export") if new else 0
        label = self.cfg.get("group_labels", {}).get(gid, "") or self.subjects.get(gid, "") or name
        first = datetime.fromtimestamp(min(r["ts"] for r in new), self.tz).strftime("%d.%m.%Y") if new else "-"
        last = datetime.fromtimestamp(max(r["ts"] for r in new), self.tz).strftime("%d.%m.%Y") if new else "-"
        self._reply(reply_to, wa_core.T("import_done", group=label, n=len(new), first=first, last=last, dup=dup, facts=n_facts))
        self._mark(mid)

    def _mark(self, mid: str):
        if mid:
            self.done.update(lambda d: {"processed": (d.get("processed", []) + [mid])[-2000:]})

    def _reply(self, to: str, text: str):
        wa_send.whatsapp_send(self.cfg, "text", text=text, log=self.log, to=to)
