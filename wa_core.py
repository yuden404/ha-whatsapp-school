"""Pure logic for the WhatsApp -> school summary pipeline.

Python port of custom_templates/whatsapp.jinja. No Home Assistant / AppDaemon
imports here, so everything is unit-testable with plain pytest.
No personal data: group ids, child names etc. come from config at runtime.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

WEEKDAYS_HE = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]  # Sunday-first
ESTIMATED_MARK = "ללא תאריך בהודעה"

_PARENS = re.compile(r"\(.*?\)")
_BOOK_PREFIX = re.compile(r"^(ספר|חוברת|חוברות)\s+")
_NOISE = re.compile(r"[\s\"״'׳\x60.,:;\-–—]")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- groups
def monitored_groups(groups: Mapping[str, str], extra: str | None = "") -> list[str]:
    """Fixed groups from config ∪ comma-separated extra ids (e.g. an input_text)."""
    out: list[str] = []
    for gid in list(groups.keys()) + str(extra or "").split(","):
        gid = str(gid).strip()
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
    for i, k in enumerate(keys):
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
    return WEEKDAYS_HE[(dd.weekday() + 1) % 7] if dd else ""


def day_label(d, ref=None) -> str:
    """'יום ראשון 4.10' + ' (היום)'/' (מחר)' relative to ref. Invalid -> ''."""
    dd = _parse(d)
    if not dd:
        return ""
    r = _parse(ref) or date.today()
    rel = " (היום)" if dd == r else " (מחר)" if dd == r + timedelta(days=1) else ""
    return f"יום {weekday_he(dd)} {dd.day}.{dd.month}{rel}"


def next_school_day(ref=None) -> str:
    """Next day after ref that is not Saturday (holidays handled by morning carry-over)."""
    n = (_parse(ref) or date.today()) + timedelta(days=1)
    if n.weekday() == 5:  # Saturday
        n += timedelta(days=1)
    return n.isoformat()


def morning_items(items, day: str, no_school: bool) -> list[dict]:
    """No-school day: only items explicitly dated today.
    School day: today's items + estimated-date items whose date passed (carry over)."""
    out = []
    for it in items if isinstance(items, list) else []:
        due = it.get("due")
        if not due:
            continue
        est = ESTIMATED_MARK in str(it.get("description") or "")
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
