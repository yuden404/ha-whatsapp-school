"""Pure logic for the WhatsApp -> school summary pipeline.

Python port of custom_templates/whatsapp.jinja. No Home Assistant / AppDaemon
imports here, so everything is unit-testable with plain pytest.
No personal data: group ids, child names etc. come from config at runtime.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from collections.abc import Iterable, Mapping

# ---------------------------------------------------------------- language
_LOCALE_DIR = __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "locales")
_L: dict = {}
_ALL_MARKS: list[str] = []  # estimated-date markers of every locale, so old items are still recognised after a switch


def set_locale(code: str = "he", locale_dir: str | None = None) -> None:
    """Load locales/<code>.json: every user-facing string the apps produce."""
    import json
    import os
    global _L, _ALL_MARKS
    d = locale_dir or _LOCALE_DIR
    with open(os.path.join(d, f"{code}.json"), encoding="utf-8") as f:
        _L = json.load(f)
    marks = []
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                marks.append(json.load(f).get("estimated_mark", ""))
    _ALL_MARKS = [m for m in marks if m]


def T(key: str, **kw) -> str:
    return _L[key].format(**kw) if kw else _L[key]


set_locale("he")

_PARENS = re.compile(r"\(.*?\)")
_BOOK_PREFIX = re.compile(r"^(ספר|חוברת|חוברות)\s+")
_NOISE = re.compile(r"[\s\"״'׳\x60.,:;\-–—]")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- groups
def monitored_groups(groups: Mapping[str, str], extra: str | None = "") -> list[str]:
    """Fixed groups from config ∪ comma-separated extra ids (e.g. an input_text)."""
    out: list[str] = []
    for raw in list(groups.keys()) + str(extra or "").split(","):
        gid = str(raw).strip()
        if gid and gid not in out:
            out.append(gid)
    return out


def child_for_chat(groups: Mapping[str, str], chat_id: str) -> str:
    """'1203...@g.us' -> child key ('' if not monitored)."""
    return groups.get(str(chat_id).split("@")[0], "")


# ---------------------------------------------------------------- paths
def safe_path(p: str) -> bool:
    """ASCII-only relative path, no traversal (Hebrew filenames crash ai_task)."""
    p = str(p)
    return bool(_SAFE_PATH.match(p)) and ".." not in p


# ---------------------------------------------------------------- dedupe
def norm(x: str) -> str:
    s = _PARENS.sub("", str(x)).strip()
    s = _BOOK_PREFIX.sub("", s)
    return _NOISE.sub("", s).lower()


def dedupe(items: Iterable[str] | None) -> list[str]:
    """Remove duplicates: equal after normalisation, or contained in a longer item.
    Keeps the longest variant, in order of first appearance."""
    if not isinstance(items, (list, tuple)):
        return []
    xs = [str(x).strip() for x in items if str(x).strip()]
    keys = [norm(x) for x in xs]
    out, seen = [], set()
    for k in keys:
        if not k or k in seen:
            continue
        if len(k) >= 3 and any(k2 != k and k in k2 for k2 in keys):
            continue  # swallowed by a longer item
        best = max((xs[j] for j, k2 in enumerate(keys) if k2 == k), key=len)
        seen.add(k)
        out.append(best)
    return out


# ---------------------------------------------------------------- dates
def _parse(d) -> date | None:
    if isinstance(d, date):
        return d
    s = str(d or "")
    if not _ISO_DATE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def weekday_he(d) -> str:
    dd = _parse(d)
    return _L["weekdays"][(dd.weekday() + 1) % 7] if dd else ""


def day_label(d, ref=None) -> str:
    """'יום ראשון 4.10' + ' (היום)'/' (מחר)' relative to ref. Invalid -> ''."""
    dd = _parse(d)
    if not dd:
        return ""
    r = _parse(ref) or date.today()
    rel = T("rel_today") if dd == r else T("rel_tomorrow") if dd == r + timedelta(days=1) else ""
    return T("day_label", weekday=weekday_he(dd), date=f"{dd.day}.{dd.month}", rel=rel)


def next_school_day(ref=None) -> str:
    """Next day after ref that is not Saturday (holidays handled by morning carry-over)."""
    n = (_parse(ref) or date.today()) + timedelta(days=1)
    if n.weekday() == 5:  # Saturday
        n += timedelta(days=1)
    return n.isoformat()


def is_estimated(description) -> bool:
    """Was this task's date guessed (no date in the message)? Recognises the marker of every locale."""
    d = str(description or "")
    return any(m in d for m in _ALL_MARKS)


def estimated_mark() -> str:
    return T("estimated_mark")


def morning_items(items, day: str, no_school: bool) -> list[dict]:
    """No-school day: only items explicitly dated today.
    School day: today's items + estimated-date items whose date passed (carry over)."""
    out = []
    for it in items if isinstance(items, list) else []:
        due = it.get("due")
        if not due:
            continue
        est = is_estimated(it.get("description"))
        if (due == day and not (no_school and est)) or (not no_school and est and due < day):
            out.append(it)
    return out


# ---------------------------------------------------------------- validation
def valid_forms(raw, source_text: str) -> list[dict]:
    """Keep only form links that appear verbatim in the source (models invent URLs)."""
    out, seen = [], set()
    for f in raw if isinstance(raw, list) else []:
        url = str(f.get("url") or "").strip()
        if url.startswith("http") and url in source_text and url not in seen:
            seen.add(url)
            due = f.get("due") or ""
            out.append({"child": f.get("child"), "title": f.get("title"), "url": url,
                        "due": due if _ISO_DATE.match(str(due)) else ""})
    return out


def parent_events(raw, today: str) -> list[dict]:
    """Only parents_required, valid future dates, HH:MM times."""
    hhmm = re.compile(r"^\d{1,2}:\d{2}$")
    out = []
    for e in raw if isinstance(raw, list) else []:
        d = str(e.get("date") or "")
        if not (e.get("parents_required") and _ISO_DATE.match(d) and d >= today):
            continue
        st, en = str(e.get("start") or ""), str(e.get("end") or "")
        out.append({"child": e.get("child"), "title": e.get("title"), "date": d,
                    "start": st if hhmm.match(st) else "", "end": en if hhmm.match(en) else "",
                    "location": e.get("location") or "", "evidence": e.get("evidence") or ""})
    return out


# ---------------------------------------------------------------- ingest
SYSTEM_TYPES = {"e2e_notification", "gp2", "notification", "notification_template", "protocol",
                "revoked", "ciphertext", "call_log", "broadcast_notification"}

_KIND_RULES = [
    ("emergency", re.compile(r"(נעדר|נעדרת|נעלם|נעלמה|המשטרה מבקשת|לאיתור|באיתור הילד|באיתור הילדה|אבד הקשר|חטיפה|נחטף|נחטפה|אירוע ביטחוני|הישארו בבית|להישאר במרחב המוגן)")),
    ("signup", re.compile(r"(מי מביא|תרשמו|להירשם|נרשמים|רשימת (הבאה|מה להביא|כיבוד)|רשימה[^\n]{0,40}(להביא|מביא|כיבוד|מפגש|ערב כיתה|מסיבה))")),
    ("urgent", re.compile(r"(אין (גן|לימודים|מעון|צהרון)|בוטל|מבוטל|סגור(ה|ים)? מחר|לא יתקיים|דחוף)")),
]


def message_kind(body: str, names: Iterable[str] = ()) -> str:
    """Real-time alert class: emergency / signup / urgent / name / none (first match wins)."""
    body = str(body or "")
    for kind, rx in _KIND_RULES:
        if rx.search(body):
            return kind
    names = [n for n in names if n]
    if names and re.search("(" + "|".join(map(re.escape, names)) + ")", body):
        return "name"
    return "none"


def accept_message(event: str, payload: Mapping, monitored: Iterable[str]) -> tuple[bool, str]:
    """Same filter as the production webhook automation. Returns (accepted, reason)."""
    if event != "message":
        return False, "not_message"
    chat = str(payload.get("from") or "")
    if not chat.endswith("@g.us") or chat.split("@")[0] not in set(monitored):
        return False, "not_monitored"
    mtype = (payload.get("_data") or {}).get("type") or "chat"
    if mtype in SYSTEM_TYPES:
        return False, "system"
    if not str(payload.get("body") or "") and not payload.get("hasMedia"):
        return False, "empty"
    return True, ""


def media_rel_path(payload: Mapping, now: datetime | None = None, root: str = "whatsapp") -> str | None:
    """ASCII-only path for a downloadable PDF/image, or None."""
    media = payload.get("media") or {}
    mime = str(media.get("mimetype") or "")
    if not payload.get("hasMedia") or not media.get("url") or not (mime == "application/pdf" or mime.startswith("image/")):
        return None
    ext = "pdf" if mime == "application/pdf" else re.sub(r"[^a-z0-9]", "", mime.split("/")[1].replace("jpeg", "jpg"))
    parts = str(payload.get("id") or "").split("_")
    hid = re.sub(r"[^A-Za-z0-9]", "", parts[2] if len(parts) > 2 else str(int((now or datetime.now()).timestamp())))
    n = now or datetime.now()
    return f"{root}/{n:%Y-%m}/wa_{n:%Y-%m-%d}_{hid[:10]}.{ext}"


# ---------------------------------------------------------------- summary
def queue_item_text(payload: Mapping) -> tuple[str, str]:
    """(sender, 'chat_id | sender: text') exactly like the production queue item."""
    data = payload.get("_data") or {}
    sender = data.get("notifyName") or str(payload.get("participant") or "")
    sender = sender.replace("@c.us", "")
    body = str(payload.get("body") or "")
    text = body if body else T("media_item", mime=(payload.get("media") or {}).get("mimetype") or T("file"))
    if len(text) > 600:
        text = text[:599] + "…"
    return sender, f"{str(payload.get('from') or '').split('@')[0]} | {sender}: {text}"


def task_due(item: Mapping, today: str) -> tuple[str, bool]:
    """(due_date, estimated). Undated items go to the next school day."""
    d = str(item.get("date") or "")
    return (d, False) if _ISO_DATE.match(d) else (next_school_day(today), True)


def fill(template: str, values: Mapping[str, str]) -> str:
    """Plain {KEY} replacement (prompts contain JSON braces, so no str.format)."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def summary_message(items, forms, events, inbox_count: int, today: str) -> str:
    """Port of the production summary message: grouped by day, then undated, parent events, forms."""
    items, forms, events = items or [], forms or [], events or []
    if not items and not forms and not events:
        return T("summary_empty", n=inbox_count)
    dated = sorted([i for i in items if _ISO_DATE.match(str(i.get("date") or ""))], key=lambda i: i["date"])
    undated = [i for i in items if i not in dated]
    out = ""
    seen_dates: list[str] = []
    for it in dated:
        if it["date"] not in seen_dates:
            seen_dates.append(it["date"])
    for d in seen_dates:
        out += f"📅 {day_label(d, today)}\n"
        out += "".join(f"• {i.get('child')} — {i.get('action')}\n" for i in dated if i["date"] == d)
        out += "\n"
    if undated:
        if dated:
            out += T("summary_undated") + "\n"
        out += "".join(f"• {i.get('child')} — {i.get('action')}\n" for i in undated)
    if events:
        out += "\n" + T("summary_events") + "\n"
        for e in events:
            out += f"• {e.get('child')} — {e.get('title')} · {day_label(e['date'], today)}"
            out += (" " + e["start"]) if e.get("start") else ""
            out += (" · " + e["location"]) if e.get("location") else ""
            out += "\n"
    if forms:
        out += "\n" + T("summary_forms") + "\n"
        for f in forms:
            due = T("summary_form_due", date=day_label(f["due"], today)) if f.get("due") else ""
            out += f"• {f.get('child')} — {f.get('title')}{due}\n  {f.get('url')}\n"
    return out.strip()  # identical to a rendered (stripped) Home Assistant template


# ---------------------------------------------------------------- prompts
def load_prompt(prompt_dir: str, name: str, schema: str | None = None) -> dict:
    """prompts/<name>.md (+ optional prompts/<schema>.schema.json) -> {"instructions", "structure"}."""
    import json
    import os
    with open(os.path.join(prompt_dir, f"{name}.md"), encoding="utf-8") as f:
        out = {"instructions": f.read()}
    if schema:
        with open(os.path.join(prompt_dir, f"{schema}.schema.json"), encoding="utf-8") as f:
            out["structure"] = json.load(f)
    return out


# ---------------------------------------------------------------- morning / daily schedule
def short_date(d) -> str:
    dd = _parse(d)
    return f"{dd.day}.{dd.month}" if dd else ""


def tasks_for_kid(tasks, day: str, kid_name: str) -> list[str]:
    """Todo summaries due today that start with the kid's name or 'כולם'."""
    rx = re.compile(r"^(" + re.escape(kid_name) + "|" + re.escape(T("everyone")) + ")")
    return [t["summary"] for t in (tasks or []) if t.get("due") == day and rx.search(str(t.get("summary") or ""))]


def plan_day(plan: Mapping | None, day: str) -> dict | None:
    """The stored weekly-plan entry for the given day, or None."""
    for d in (plan or {}).get("days") or []:
        if d.get("date") == day:
            return d
    return None


def plan_active(pointer: str, day: str) -> tuple[bool, str]:
    """input_text pointer 'file|start|end|title' -> (covers day, file)."""
    parts = str(pointer or "").split("|")
    ok = len(parts) >= 3 and parts[1] <= day <= parts[2] and safe_path(parts[0])
    return ok, parts[0] if ok else ""


def daily_message(kid_name: str, day: str, entry: Mapping | None, today_tasks: list[str],
                  has_plan: bool, school_child: bool, test: bool = False) -> dict | None:
    """Port of the 07:15 message. Returns {"kind", "title", "body", "push", "telegram"} or None (nothing to send)."""
    pre = "🧪 " if test else ""
    wd, dm = weekday_he(day), short_date(day)
    is_friday = (_parse(day) or date.today()).weekday() == 4
    if not has_plan:
        if school_child and not is_friday:
            text = T("daily_no_plan", pre=pre, kid=kid_name, weekday=wd, date=dm)
            return {"kind": "no_plan", "title": "", "body": text, "push": None, "telegram": text}
        return None
    if entry is None:
        title = T("daily_fail_title", pre=pre, kid=kid_name, weekday=wd, date=dm)
        body = T("daily_fail_body")
        if today_tasks:
            body += "\n" + T("daily_from_groups") + "\n" + "".join(f"• {t}\n" for t in today_tasks)
        body = body.rstrip("\n")
        return {"kind": "fail", "title": title, "body": body, "push": body, "telegram": f"{title}\n{body}"}
    lessons, bring, notes = entry.get("lessons") or [], entry.get("bring") or [], entry.get("notes") or []
    if entry.get("no_school") and not lessons:
        return None
    title = T("daily_title", pre=pre, kid=kid_name, weekday=wd, date=dm)
    body = ""
    if entry.get("hours"):
        body += T("daily_hours", hours=entry["hours"]) + "\n"
    if lessons:
        body += T("daily_lessons") + "\n" + "".join(f"{i}. {lesson}\n" for i, lesson in enumerate(lessons, 1))
    if bring:
        body += "\n" + T("daily_bring") + "\n" + "".join(f"• {b}\n" for b in bring)
    if notes:
        body += "\n" + T("daily_notes") + "\n" + "".join(f"• {n}\n" for n in notes)
    if today_tasks:
        body += "\n" + T("daily_from_groups") + "\n" + "".join(f"• {t}\n" for t in today_tasks)
    body = body.rstrip("\n")  # Home Assistant strips rendered templates; keep messages identical
    push = T("push_bring", items=", ".join(bring)) if bring else T("push_no_bring")
    if notes:
        push += f"\n📝 {' · '.join(notes)}"
    if today_tasks:
        push += f"\n📌 {' · '.join(today_tasks)}"
    push += "\n" + T("push_lessons", n=len(lessons))
    return {"kind": "school", "title": title, "body": body, "push": push, "telegram": f"{title}\n\n{body}"}


def morning_message(items) -> str:
    return "".join(f"• {it['summary']}\n" for it in items)



def telegram_safe(text: str) -> str:
    """Telegram parses notify messages as Markdown: a lone _ * [ or backtick makes the send fail silently."""
    return str(text).replace("_", "-").replace("*", "×").replace("[", "(").replace("]", ")").replace(chr(96), "'")



# ---------------------------------------------------------------- real-time alerts
ALERT_ICON = {"emergency": "🚨", "signup": "📝", "urgent": "⚠️", "name": "👧"}


def jinja_truncate(s: str, length: int, end: str = "…", leeway: int = 5) -> str:
    """Same result as Jinja's truncate(length, killwords=True, end, leeway=5)."""
    s = str(s)
    return s if len(s) <= length + leeway else s[: length - len(end)] + end


def is_daytime(t: datetime) -> bool:
    """Alerts and read receipts are immediate between 06:00 and 23:00, deferred to the morning otherwise."""
    return 6 <= t.hour < 23


def day_alert(kind: str, group_label: str, sender: str, body: str) -> dict | None:
    titles = _L["alert_title"]
    if kind not in titles:
        return None
    # .strip(): Home Assistant strips rendered templates, keep the text identical to production
    return {"title": (titles[kind] + group_label).strip(), "text": f"{sender}: {jinja_truncate(body, 300)}".strip()}


def night_alert_message(items) -> str:
    """Morning message for alert-worthy messages that arrived at night. items: [{"kind", "item"}]."""
    out = ""
    for x in items:
        if x.get("kind") in ALERT_ICON:
            text = x["item"].split(" | ", 1)[1] if " | " in x["item"] else x["item"]
            out += f"{ALERT_ICON[x['kind']]} {jinja_truncate(text, 250)}\n"
    return out.strip()
