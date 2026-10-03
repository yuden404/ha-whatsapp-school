"""App-level tests on a fake Home Assistant: the shadow pipeline end to end, no network, no model."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import fake_hass  # noqa: E402

GROUPS = {"111": "kid_a", "222": "kid_b"}


def _ingest(tmp, services=None, states=None):
    args = {"shadow": True, "groups": GROUPS, "groups_helper": "input_text.g", "alert_names": ["Dana"],
            "data_dir": tmp, "media_root": "wa_test_media", "waha_url": "http://waha", "waha_header_file": "/dev/null",
            "ai_task_entity": "ai_task.x", "alert_group_labels": {"111": "Class A"}}
    return fake_hass.make("wa_ingest", "WaIngest", args, states or {"input_text.g": {"state": ""}}, services or {})


def _msg(mid="m1", chat="111@g.us", body="hello", **extra):
    return {"json": json.dumps({"event": "message", "payload": {"id": mid, "from": chat, "body": body, "timestamp": 1700000000,
                                                                 "hasMedia": False, "_data": {"type": "chat", "notifyName": "Teacher"}, **extra}})}


def test_ingest_filters_and_enqueues():
    tmp = tempfile.mkdtemp()
    app = _ingest(tmp)
    app.on_webhook("wa_shadow_webhook", _msg(), {})                      # accepted
    app.on_webhook("wa_shadow_webhook", _msg("m2", chat="999@g.us"), {})  # not monitored
    app.on_webhook("wa_shadow_webhook", _msg("m3", body=""), {})          # empty
    q = [json.loads(l) for l in open(os.path.join(tmp, "queue.jsonl"), encoding="utf-8")]
    assert [x["id"] for x in q] == ["m1"] and q[0]["item"] == "111 | Teacher: hello" and q[0]["kind"] == "none"


def test_ingest_compares_with_production():
    tmp = tempfile.mkdtemp()
    app = _ingest(tmp)
    app.on_webhook("wa_shadow_webhook", _msg(body="Dana forgot her hat"), {})
    app.on_prod("wa_prod_ingest", {"id": "m1", "kind": "name", "local": "", "ftext_len": "0", "weekly": "False", "start": "", "end": "", "ndays": "0"}, {})
    lines = [json.loads(l) for l in open(os.path.join(tmp, "compare", sorted(os.listdir(os.path.join(tmp, "compare")))[-1]), encoding="utf-8")]
    assert lines[-1]["part"] == "ingest" and lines[-1]["ok"], lines[-1]
    app.on_prod("wa_prod_ingest", {"id": "m9", "kind": "none"}, {})  # production saw a message the shadow never got
    app.fire_timers()                                                # ... reported when the wait times out
    lines = [json.loads(l) for l in open(os.path.join(tmp, "compare", sorted(os.listdir(os.path.join(tmp, "compare")))[-1]), encoding="utf-8")]
    assert not lines[-1]["ok"] and "only_prod" in lines[-1]["diffs"]


def test_ingest_day_alert_matches_production_format():
    tmp = tempfile.mkdtemp()
    app = _ingest(tmp)
    import wa_core
    daytime = wa_core.is_daytime(__import__("datetime").datetime.now())
    app.on_webhook("wa_shadow_webhook", _msg(body="דחוף: מחר אין לימודים"), {})
    if daytime:
        assert app.alerts["m1"] == {"title": "⚠️ דחוף — Class A", "text": "Teacher: דחוף: מחר אין לימודים"}
        app.on_prod_alert("wa_prod_alert", {"id": "m1", "title": "⚠️ דחוף — Class A", "text": "Teacher: דחוף: מחר אין לימודים"}, {})
        lines = [json.loads(l) for l in open(os.path.join(tmp, "compare", sorted(os.listdir(os.path.join(tmp, "compare")))[-1]), encoding="utf-8")]
        assert lines[-1]["part"] == "alert" and lines[-1]["ok"]
    else:
        assert "m1" not in app.alerts  # night: deferred to the 06:00 digest


def test_summary_builds_message_from_fake_model():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "compare"))
    with open(os.path.join(tmp, "queue.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "m1", "ts": 1700000000, "chat": "111@g.us", "sender": "Teacher",
                            "item": "111 | Teacher: tomorrow bring a ruler. form: https://forms.gle/abc", "ftext": "", "media": None, "mime": "", "kind": "none", "alerted": False}) + "\n")
    model = {"tasks": [{"child": "Dana", "date": "", "action": "להביא סרגל", "source": "Class A"}],
             "forms": [{"child": "Dana", "title": "form", "url": "https://forms.gle/abc", "due": ""},
                       {"child": "Dana", "title": "invented", "url": "https://forms.gle/NOPE", "due": ""}],
             "events": []}
    services = {"ai_task/generate_data": lambda d: {"response": {"data": model}},
                "todo/get_items": lambda d: {"response": {"todo.t": {"items": []}}}}
    args = {"shadow": True, "data_dir": tmp, "ai_task_entity": "ai_task.x", "tasks_todo": "todo.t", "family_intro": "fam",
            "child_names": ["Dana"], "school_children": ["Dana"], "names_mention": "Dana", "group_labels": {"111": "Class A"}}
    app = fake_hass.make("wa_summary", "WaSummary", args, {}, services)
    app.run_summary({"slot": "evening"})
    sent = app.calls[-1][1]["service_data"]
    assert "{INBOX}" not in sent["instructions"] and "tomorrow bring a ruler" in sent["instructions"]
    assert sent["structure"]["tasks"]["selector"]["object"]["fields"]["child"]["selector"]["select"]["options"] == ["Dana", "כולם"]
    out = app.last
    assert [f["url"] for f in out["forms"]] == ["https://forms.gle/abc"]      # invented link dropped
    assert out["todo_adds"][0]["estimated"] is True                            # undated -> next school day
    assert "• Dana — להביא סרגל" in out["msg"] and "https://forms.gle/abc" in out["msg"]
    assert open(os.path.join(tmp, "queue.jsonl"), encoding="utf-8").read() == ""  # consumed


def test_morning_daily_and_night_and_report():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "compare"))
    import datetime as _dt
    today = _dt.date.today().isoformat()
    entry = {"date": today, "lessons": ["L1"], "bring": ["ruler"], "notes": [], "hours": ""}
    states = {"input_text.p": {"state": f"wa/x.pdf|2020-01-01|2099-01-01|t"},
              "sensor.plan": {"state": "x", "attributes": {"file": "wa/x.pdf", "days": [entry]}},
              "sensor.school": {"state": "x", "attributes": {"elementary_vacation": False}},
              "binary_sensor.sb": {"state": "off"}}
    services = {"todo/get_items": lambda d: {"response": {"todo.t": {"items": [{"summary": "Dana — hat", "due": today}]}}}}
    json.dump({"kid_a": {"start": "2020-01-01", "end": "2099-01-01", "days": [entry]}}, open(os.path.join(tmp, "weekly_plans.json"), "w"))
    args = {"data_dir": tmp, "tasks_todo": "todo.t", "school_calendar_sensor": "sensor.school", "issur_melacha_sensor": "binary_sensor.sb",
            "kids": [{"key": "kid_a", "name": "Dana", "school": True, "pointer": "input_text.p", "sensor": "sensor.plan"}]}
    app = fake_hass.make("wa_morning", "WaMorning", args, states, services)
    app.run_daily_schedule({})
    import wa_core
    if wa_core.weekday_he(today) != "שבת":
        par = app.daily["Dana"]["parity"]
        assert par["kind"] == "school" and "Dana — hat" in par["body"]
        app._compare_daily({"kid": "Dana", "kind": "school", "title": par["title"], "body": par["body"]})
        lines = [json.loads(l) for l in open(os.path.join(tmp, "compare", sorted(os.listdir(os.path.join(tmp, "compare")))[-1]), encoding="utf-8")]
        assert lines[-1]["part"] == "daily" and lines[-1]["ok"], lines[-1]
    app.run_morning({})
    assert app.morning == ["Dana — hat"]
    # night digest ignores messages already alerted during the day
    with open(os.path.join(tmp, "queue.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "a", "item": "111 | T: urgent one", "kind": "urgent", "alerted": True}) + "\n")
        f.write(json.dumps({"id": "b", "item": "111 | T: urgent two", "kind": "urgent", "alerted": False}) + "\n")
    night = fake_hass.make("wa_morning", "WaNight", {"data_dir": tmp}, {}, {})
    night.run_night({})
    assert night.msg == "⚠️ T: urgent two"
    # report: one message, Telegram-safe
    rep = fake_hass.make("wa_morning", "WaReport", {"data_dir": tmp, "notify_entity": "notify.x"}, {}, {})
    rep.report({})
    msg = rep.calls[-1][1]["message"]
    assert msg.startswith("🔬") and "_" not in msg
