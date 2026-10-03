"""Unit tests for wa_core. Synthetic data only, never real messages or ids.
Runs with pytest, or via wa_selftest (AppDaemon) which calls every test_* function."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wa_core as c  # noqa: E402

GROUPS = {"111": "kid_a", "222": "kid_a", "333": "kid_b"}


# --- dedupe
def test_dedupe_containment_keeps_longest():
    assert c.dedupe(["קסם וחברים 1", "חוברת קסם וחברים 1", "מחברת", "מחברת שפה"]) == ["חוברת קסם וחברים 1", "מחברת שפה"]


def test_dedupe_exact_and_parentheses():
    assert c.dedupe(["סווטשרט", "סווטשרט ", "כובע (לשעה 5)", "כובע"]) == ["סווטשרט", "כובע (לשעה 5)"]


def test_dedupe_invalid_input():
    assert c.dedupe(None) == [] and c.dedupe("abc") == []


def test_dedupe_short_item_not_swallowed_wrongly():
    assert len(c.dedupe(["מים", "בקבוק מים"])) == 1
    assert len(c.dedupe(["אב", "אבטיח"])) == 2


def test_dedupe_book_equals_booklet():
    assert c.dedupe(["חוברת שבילים 5", "ספר שבילים 5", "סרגל"]) == ["חוברת שבילים 5", "סרגל"]


# --- groups
def test_monitored_empty_extra():
    assert len(c.monitored_groups(GROUPS, "")) == 3


def test_monitored_extra_spaces_and_dupes():
    assert len(c.monitored_groups(GROUPS, " 999 , 111,,")) == 4


def test_monitored_returns_list_not_tuple():
    assert isinstance(c.monitored_groups(GROUPS, "1,2"), list)


def test_child_for_chat():
    assert c.child_for_chat(GROUPS, "333@g.us") == "kid_b"
    assert c.child_for_chat(GROUPS, "444@g.us") == ""


# --- paths
def test_safe_path_ascii_ok():
    assert c.safe_path("whatsapp/2026-10/wa_2026-10-02_X.pdf")


def test_safe_path_hebrew_blocked():
    assert not c.safe_path("whatsapp/תוכנית.pdf")


def test_safe_path_traversal_blocked():
    assert not c.safe_path("whatsapp/../secrets.yaml")


# --- dates
def test_day_label_weekday():
    assert c.day_label("2026-10-04", "2026-10-01") == "יום ראשון 4.10"


def test_day_label_10_stays_10():  # Jinja turned "4.10" into 4.1
    assert c.day_label("2026-10-04", "2026-10-01").endswith("4.10")


def test_day_label_tomorrow_only_next_day():
    assert c.day_label("2026-10-03", "2026-10-02") == "יום שבת 3.10 (מחר)"
    assert "(מחר)" not in c.day_label("2026-10-04", "2026-10-02")


def test_day_label_invalid():
    assert c.day_label("", "2026-10-02") == "" and c.day_label("xx") == ""


def test_next_school_day_friday_to_sunday():
    assert c.next_school_day("2026-10-02") == "2026-10-04"


def test_next_school_day_thursday_to_friday():
    assert c.next_school_day("2026-10-01") == "2026-10-02"


# --- morning
ITEMS = [
    {"summary": "א", "due": "2026-10-03", "description": "כיתה · ללא תאריך בהודעה"},
    {"summary": "ב", "due": "2026-10-03", "description": "כיתה"},
    {"summary": "ג", "due": "2026-10-02", "description": "כיתה · ללא תאריך בהודעה"},
    {"summary": "ד", "due": "2026-10-02", "description": "כיתה"},
]


def _s(xs):
    return [x["summary"] for x in xs]


def test_morning_saturday_only_explicit():
    assert _s(c.morning_items(ITEMS, "2026-10-03", True)) == ["ב"]


def test_morning_school_day_carry_over():
    assert _s(c.morning_items(ITEMS, "2026-10-04", False)) == ["א", "ג"]


def test_morning_school_day_today_plus_overdue_estimated():
    assert _s(c.morning_items(ITEMS, "2026-10-03", False)) == ["א", "ב", "ג"]


# --- validation
def test_forms_url_must_appear_in_source():
    raw = [{"child": "x", "title": "t", "url": "https://forms.gle/Real1", "due": "2026-10-06"},
           {"child": "x", "title": "t", "url": "https://forms.gle/Invented", "due": ""}]
    out = c.valid_forms(raw, "נא למלא https://forms.gle/Real1 עד שלישי")
    assert [f["url"] for f in out] == ["https://forms.gle/Real1"]


def test_forms_bad_due_cleared():
    out = c.valid_forms([{"url": "https://a.b/c", "due": "שלישי"}], "https://a.b/c")
    assert out[0]["due"] == ""


def test_parent_events_filter():
    raw = [{"title": "טקס", "date": "2026-10-08", "start": "12:00", "parents_required": True},
           {"title": "מסיבה לילדים", "date": "2026-10-11", "parents_required": False},
           {"title": "עבר", "date": "2026-09-01", "parents_required": True},
           {"title": "שעה שבורה", "date": "2026-10-09", "start": "בצהריים", "parents_required": True}]
    out = c.parent_events(raw, "2026-10-03")
    assert [e["title"] for e in out] == ["טקס", "שעה שבורה"]
    assert out[1]["start"] == ""


# --- ingest
def test_kind_rules_order():
    assert c.message_kind("דחוף: מחר אין גן") == "urgent"
    assert c.message_kind("מי מביא חטיפים?") == "signup"
    assert c.message_kind("ילד נעדר מהגן, המשטרה מבקשת") == "emergency"
    assert c.message_kind("תודה רבה", ["דנה"]) == "none"
    assert c.message_kind("דנה שכחה כובע", ["דנה"]) == "name"


def test_accept_filters():
    mon = ["111"]
    base = {"from": "111@g.us", "body": "שלום"}
    assert c.accept_message("message", base, mon) == (True, "")
    assert c.accept_message("session.status", base, mon)[1] == "not_message"
    assert c.accept_message("message", {**base, "from": "999@g.us"}, mon)[1] == "not_monitored"
    assert c.accept_message("message", {**base, "_data": {"type": "revoked"}}, mon)[1] == "system"
    assert c.accept_message("message", {"from": "111@g.us", "body": ""}, mon)[1] == "empty"
    assert c.accept_message("message", {"from": "111@g.us", "body": "", "hasMedia": True}, mon)[0]


def test_media_rel_path():
    from datetime import datetime as dt
    p = {"hasMedia": True, "id": "false_111@g.us_FAKE0HASH0123456_2@lid",
         "media": {"url": "http://x/a", "mimetype": "application/pdf"}}
    assert c.media_rel_path(p, dt(2026, 10, 2)) == "whatsapp/2026-10/wa_2026-10-02_FAKE0HASH0.pdf"
    assert c.media_rel_path({**p, "media": {"url": "u", "mimetype": "video/mp4"}}) is None
    assert c.safe_path(c.media_rel_path({**p, "media": {"url": "u", "mimetype": "image/jpeg"}}, dt(2026, 1, 1)))
