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
    assert c.media_rel_path({**p, "media": {"url": "u", "mimetype": "video/mp4"}}, dt(2026, 1, 1)).endswith(".mp4")
    assert c.media_rel_path({**p, "media": {"url": "u", "mimetype": "audio/ogg; codecs=opus"}}, dt(2026, 1, 1)).endswith(".ogg")
    assert c.media_rel_path({**p, "media": {"url": "u", "mimetype": "application/zip"}}) is None
    assert c.safe_path(c.media_rel_path({**p, "media": {"url": "u", "mimetype": "image/jpeg"}}, dt(2026, 1, 1)))


# --- summary
def test_queue_item_text():
    p = {"from": "111@g.us", "body": "שלום", "_data": {"notifyName": "מורה"}}
    assert c.queue_item_text(p) == ("מורה", "111 | מורה: שלום")
    p2 = {"from": "111@g.us", "body": "", "participant": "972500000000@c.us", "media": {"mimetype": "application/pdf"}}
    assert c.queue_item_text(p2)[1] == "111 | 972500000000: [מדיה: application/pdf]"


def test_task_due_undated_goes_to_next_school_day():
    assert c.task_due({"date": ""}, "2026-10-02") == ("2026-10-04", True)
    assert c.task_due({"date": "2026-10-07"}, "2026-10-02") == ("2026-10-07", False)


def test_fill_keeps_json_braces():
    assert c.fill('{"a": 1} {X}', {"X": "y"}) == '{"a": 1} y'


def test_summary_message_grouped():
    items = [{"child": "A", "date": "2026-10-07", "action": "כובע"}, {"child": "A", "date": "2026-10-03", "action": "x"},
             {"child": "B", "date": "", "action": "סווטשרט"}, {"child": "B", "date": "2026-10-07", "action": "תחפושת"}]
    m = c.summary_message(items, [], [], 3, "2026-10-02")
    assert m == ("📅 יום שבת 3.10 (מחר)\n• A — x\n\n📅 יום רביעי 7.10\n• A — כובע\n• B — תחפושת\n\n"
                 "📌 בלי תאריך\n• B — סווטשרט")


def test_summary_message_empty_and_sections():
    assert c.summary_message([], [], [], 5, "2026-10-02") == "אין חדש מהקבוצות (5 הודעות נבדקו)."
    m = c.summary_message([], [{"child": "A", "title": "אישור", "url": "https://f/x", "due": "2026-10-06"}],
                          [{"child": "A", "title": "טקס", "date": "2026-10-08", "start": "12:00", "location": ""}], 1, "2026-10-02")
    assert "👨‍👩‍👧 נוכחות הורים" in m and "• A — טקס · יום חמישי 8.10 12:00" in m
    assert "• A — אישור (עד יום שלישי 6.10)\n  https://f/x" in m


# --- prompts
def test_prompts_have_no_unfilled_jinja_and_known_placeholders():
    import re as _re
    pdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
    allowed = {"FAMILY_INTRO", "TODAY_DMY", "TODAY_WD", "TOMORROW", "GROUP_MAP", "INBOX", "EXISTING", "FILES_NOTE",
               "SCHOOL_CHILDREN", "NAMES_MENTION", "CHILD_OPTIONS", "OUTPUT_LANGUAGE", "TODAY", "N", "FILE_MAP"}
    for fn in os.listdir(pdir):
        if fn.endswith(".md"):
            with open(os.path.join(pdir, fn), encoding="utf-8") as fh:
                t = fh.read()
            assert "{{" not in t and "{%" not in t, fn
            assert set(_re.findall(r"\{([A-Z_]+)\}", t)) <= allowed, fn


# --- morning / daily
def test_tasks_for_kid():
    t = [{"summary": "דנה — סרגל", "due": "2026-10-05"}, {"summary": "כולם — כובע", "due": "2026-10-05"},
         {"summary": "רון — x", "due": "2026-10-05"}, {"summary": "דנה — y", "due": "2026-10-06"}]
    assert c.tasks_for_kid(t, "2026-10-05", "דנה") == ["דנה — סרגל", "כולם — כובע"]


def test_plan_active():
    assert c.plan_active("whatsapp/a.pdf|2026-10-04|2026-10-09|t", "2026-10-05") == (True, "whatsapp/a.pdf")
    assert c.plan_active("whatsapp/a.pdf|2026-10-04|2026-10-09", "2026-10-10")[0] is False
    assert c.plan_active("unknown", "2026-10-05")[0] is False


def test_daily_message_school_day():
    e = {"date": "2026-10-05", "hours": "", "lessons": ["שעה 1: חשבון", "שעה 2: גיאומטריה"], "bring": ["סרגל"], "notes": []}
    m = c.daily_message("דנה", "2026-10-05", e, ["דנה — חולצה לבנה"], True, True)
    assert m["title"] == "🎒 דנה — יום שני 5.10"
    assert m["body"] == "📚 המערכת:\n1. שעה 1: חשבון\n2. שעה 2: גיאומטריה\n\n🎒 להביא:\n• סרגל\n\n📌 מהקבוצות:\n• דנה — חולצה לבנה"
    assert m["push"] == "🎒 סרגל\n📌 דנה — חולצה לבנה\n📚 2 שיעורים — פתח למערכת המלאה"


def test_daily_message_no_plan_rules():
    assert c.daily_message("דנה", "2026-10-05", None, [], False, True)["kind"] == "no_plan"
    assert c.daily_message("דנה", "2026-10-09", None, [], False, True) is None   # Friday
    assert c.daily_message("רון", "2026-10-05", None, [], False, False) is None  # kindergarten child
    assert c.daily_message("דנה", "2026-10-05", None, [], True, True)["kind"] == "fail"
    assert c.daily_message("דנה", "2026-10-05", {"no_school": True, "lessons": []}, [], True, True) is None


def test_morning_message():
    assert c.morning_message([{"summary": "א"}, {"summary": "ב"}]) == "• א\n• ב\n"



def test_telegram_safe():
    s = c.telegram_safe("integration/gemini_structured_output: only_shadow [x] *y*")
    assert not any(ch in s for ch in "_*[]") and "gemini-structured-output" in s



# --- alerts
def test_jinja_truncate_matches_jinja():
    assert c.jinja_truncate("a" * 305, 300) == "a" * 305          # within leeway
    assert c.jinja_truncate("a" * 306, 300) == "a" * 299 + "…"


def test_is_daytime_bounds():
    from datetime import datetime as dt
    assert not c.is_daytime(dt(2026, 10, 4, 5, 59)) and c.is_daytime(dt(2026, 10, 4, 6, 0))
    assert c.is_daytime(dt(2026, 10, 4, 22, 59)) and not c.is_daytime(dt(2026, 10, 4, 23, 0))


def test_day_alert():
    a = c.day_alert("urgent", "כיתה", "מורה", "מחר אין לימודים")
    assert a == {"title": "⚠️ דחוף — כיתה", "text": "מורה: מחר אין לימודים"}
    assert c.day_alert("none", "x", "y", "z") is None


def test_night_alert_message():
    m = c.night_alert_message([{"kind": "urgent", "item": "111 | מורה: מחר אין גן"}, {"kind": "none", "item": "111 | x: תודה"},
                               {"kind": "name", "item": "111 | אמא: דנה שכחה כובע"}])
    assert m == "⚠️ מורה: מחר אין גן\n👧 אמא: דנה שכחה כובע"



# --- queue store
def test_jsonl_queue_append_read_drop(tmp_path=None):
    import tempfile
    import wa_store
    d = tempfile.mkdtemp()
    q = wa_store.JsonlQueue(os.path.join(d, "q.jsonl"))
    assert q.read() == []
    q.append({"id": "1", "x": "א"})
    q.append({"id": "2"})
    assert [i["id"] for i in q.read()] == ["1", "2"]
    q.drop({"1"})
    assert [i["id"] for i in q.read()] == ["2"]
    q.append({"id": "3"})
    q.drop({"2", "3"})
    assert q.read() == []


# --- locales
def test_english_locale_and_switch_back():
    try:
        c.set_locale("en")
        m = c.daily_message("Dana", "2026-10-05", {"lessons": ["Math"], "bring": ["ruler"], "notes": []}, [], True, True)
        assert m["title"] == "🎒 Dana — Monday 5.10" and "📚 Schedule:" in m["body"] and "🎒 Bring:" in m["body"]
        assert c.day_label("2026-10-03", "2026-10-02") == "Saturday 3.10 (tomorrow)"
        assert c.summary_message([], [], [], 2, "2026-10-02") == "Nothing new from the groups (2 messages checked)."
        assert c.day_alert("urgent", "Class", "Teacher", "no school")["title"] == "⚠️ Urgent — Class"
        # a task marked in Hebrew is still recognised as estimated after switching language
        assert c.is_estimated("כיתה · ללא תאריך בהודעה") and c.is_estimated("x · no date in message")
    finally:
        c.set_locale("he")
    assert c.day_label("2026-10-04", "2026-10-01") == "יום ראשון 4.10"


def test_every_locale_has_the_same_keys():
    import json
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locales")
    keys = {fn: set(json.load(open(os.path.join(d, fn), encoding="utf-8"))) for fn in os.listdir(d) if fn.endswith(".json")}
    assert len(keys) >= 2 and len({frozenset(v) for v in keys.values()}) == 1, keys



# --- media policy / albums
def test_media_class_and_should_read():
    p = lambda m: {"hasMedia": True, "media": {"url": "u", "mimetype": m}}  # noqa: E731
    assert c.media_class(p("application/pdf")) == "pdf" and c.media_class(p("audio/ogg; codecs=opus")) == "audio"
    assert c.media_class(p("video/mp4")) == "video" and c.media_class({"hasMedia": False}) == "none"
    assert c.should_read("audio", "", "999@g.us", []) and c.should_read("pdf", "", "999@g.us", [])
    assert not c.should_read("image", "", "999@g.us", ["111"]) and c.should_read("image", "", "111@g.us", ["111"])
    assert c.should_read("image", "רשימת ציוד", "999@g.us", []) and not c.should_read("video", "x", "111@g.us", ["111"])


def test_collapse_albums():
    q = [{"id": "1", "chat": "111@g.us", "sender": "A", "ts": 100, "mclass": "image", "item": "x", "ftext": ""},
         {"id": "2", "chat": "111@g.us", "sender": "A", "ts": 160, "mclass": "image", "item": "x", "ftext": ""},
         {"id": "3", "chat": "111@g.us", "sender": "A", "ts": 200, "mclass": "video", "item": "x", "ftext": ""},
         {"id": "4", "chat": "111@g.us", "sender": "B", "ts": 210, "mclass": "none", "item": "111 | B: hello", "ftext": ""},
         {"id": "5", "chat": "111@g.us", "sender": "A", "ts": 5000, "mclass": "image", "item": "x", "ftext": ""},
         {"id": "6", "chat": "111@g.us", "sender": "A", "ts": 5010, "mclass": "image", "item": "x", "ftext": "רשימת ציוד"}]
    out = c.collapse_albums(q)
    assert [o["item"] for o in out] == ["111 | A: [2 תמונות + סרטון]", "111 | B: hello", "111 | A: [תמונה]", "x"]
    assert out[0]["_ids"] == ["1", "2", "3"]



def test_daily_message_tomorrow_title():
    e = {"lessons": ["L1"], "bring": [], "notes": []}
    m = c.daily_message("דנה", "2026-10-07", e, [], True, True, tomorrow=True)
    assert m["title"] == "🎒 דנה — מחר, יום רביעי 7.10"
    assert c.daily_message("דנה", "2026-10-07", e, [], True, True)["title"] == "🎒 דנה — יום רביעי 7.10"
