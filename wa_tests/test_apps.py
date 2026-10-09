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



def test_summary_live_delivers_tasks_forms_events():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "compare"))
    with open(os.path.join(tmp, "queue.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "m1", "ts": 1700000000, "chat": "111@g.us", "sender": "T", "item": "111 | T: ceremony, parents invited. https://forms.gle/abc",
                            "ftext": "", "media": None, "mime": "", "mclass": "none", "kind": "none", "alerted": False}) + "\n")
    model = {"tasks": [{"child": "Dana", "date": "", "action": "להביא סרגל", "source": "Class A"}],
             "forms": [{"child": "Dana", "title": "confirm", "url": "https://forms.gle/abc", "due": "2030-01-05"}],
             "events": [{"child": "Dana", "title": "טקס", "date": "2030-01-08", "start": "12:00", "end": "", "location": "", "parents_required": True, "evidence": "parents invited", "source": "Class A"}]}
    services = {"ai_task/generate_data": lambda d: {"response": {"data": model}},
                "todo/get_items": lambda d: {"response": {"todo.t": {"items": []}}},
                "calendar/get_events": lambda d: {"response": {"calendar.c": {"events": []}}}}
    args = {"shadow": False, "data_dir": tmp, "ai_task_entity": "ai_task.x", "tasks_todo": "todo.t", "family_intro": "fam",
            "child_names": ["Dana"], "school_children": ["Dana"], "names_mention": "Dana", "group_labels": {"111": "Class A"},
            "notify_entity": "notify.x", "calendars_helper": "input_text.cal", "whatsapp_to": None, "last_summary_helper": "input_text.last"}
    app = fake_hass.make("wa_summary", "WaSummary", args, {"input_text.cal": {"state": "calendar.c"}}, services)
    app.run_summary({"slot": "evening"})
    names = [s for s, _ in app.calls]
    assert names.count("todo/add_item") == 2 and "calendar/create_event" in names and "notify/send_message" in names
    adds = [d for s, d in app.calls if s == "todo/add_item"]
    assert adds[0]["item"] == "Dana — להביא סרגל" and "ללא תאריך בהודעה" in adds[0]["description"]
    assert adds[1]["description"] == "https://forms.gle/abc"
    cal = [d for s, d in app.calls if s == "calendar/create_event"][0]
    assert cal["start_date_time"] == "2030-01-08 12:00:00" and cal["end_date_time"] == "2030-01-08 13:00:00"
    assert open(os.path.join(tmp, "queue.jsonl"), encoding="utf-8").read() == ""


def test_ingest_live_schedules_seen_and_alert():
    tmp = tempfile.mkdtemp()
    args = {"shadow": False, "groups": GROUPS, "groups_helper": "input_text.g", "alert_names": ["Dana"],
            "data_dir": tmp, "media_root": "wa_test_media", "waha_url": "http://waha", "waha_header_file": "/dev/null",
            "ai_task_entity": "ai_task.x", "alert_group_labels": {"111": "Class A"}, "notify_entity": "notify.x"}
    app = fake_hass.make("wa_ingest", "WaIngest", args, {"input_text.g": {"state": ""}}, {})
    app.on_webhook("wa_shadow_webhook", _msg(body="דחוף: מחר אין לימודים"), {})
    import wa_core
    if wa_core.is_daytime(__import__("datetime").datetime.now()):
        delays = [d for d, _ in app.timers if isinstance(d, int)]
        assert delays and 30 <= delays[-1] <= 120          # read receipt + alert after a human-like pause
    q = [json.loads(l) for l in open(os.path.join(tmp, "queue.jsonl"), encoding="utf-8")]
    assert q[0]["kind"] == "urgent" and q[0]["mclass"] == "none"



def _summary_live(tmp, model, tasks, queue_lines=()):
    os.makedirs(os.path.join(tmp, "compare"), exist_ok=True)
    with open(os.path.join(tmp, "queue.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(x) + "\n" for x in queue_lines)
    services = {"ai_task/generate_data": lambda d: {"response": {"data": model}},
                "todo/get_items": lambda d: {"response": {"todo.t": {"items": tasks}}},
                "calendar/get_events": lambda d: {"response": {"calendar.c": {"events": []}}}}
    args = {"shadow": False, "data_dir": tmp, "ai_task_entity": "ai_task.x", "tasks_todo": "todo.t", "family_intro": "fam",
            "child_names": ["Dana"], "school_children": ["Dana"], "names_mention": "Dana", "group_labels": {"111": "Class A"},
            "notify_entity": "notify.x", "calendars_helper": "input_text.cal", "whatsapp_to": None,
            "titles": {"morning": "MORNING", "evening": "EVENING"}}
    return fake_hass.make("wa_summary", "WaSummary", args, {"input_text.cal": {"state": ""}}, services)


def _q(item="111 | T: מחר להביא סרגל"):
    return {"id": "m1", "ts": 1700000000, "chat": "111@g.us", "sender": "T", "item": item, "ftext": "", "media": None,
            "mime": "", "mclass": "none", "kind": "none", "alerted": False}


def test_morning_is_one_message_with_today_and_overnight():
    import datetime as _dt
    today = _dt.date.today().isoformat()
    tmp = tempfile.mkdtemp()
    tasks = [{"summary": "Dana — hat", "due": today, "description": ""}]
    model = {"tasks": [{"child": "Dana", "date": "", "action": "להביא סרגל", "source": "Class A"}], "forms": [], "events": []}
    app = _summary_live(tmp, model, tasks, [_q()])
    app.run_summary({"slot": "morning"})
    sent = [d["message"] for s, d in app.calls if s == "notify/send_message"]
    assert len(sent) == 1                                              # ONE message, not two
    assert sent[0].startswith("MORNING\n📌 להיום\n• Dana — hat") and "🌙 חדש מהלילה" in sent[0] and "להביא סרגל" in sent[0]
    assert sent[0].count("Dana — hat") == 1                           # the new task does not repeat the old one


def test_morning_with_empty_queue_still_lists_today():
    import datetime as _dt
    today = _dt.date.today().isoformat()
    tmp = tempfile.mkdtemp()
    app = _summary_live(tmp, {}, [{"summary": "Dana — hat", "due": today, "description": ""}], [])
    app.run_summary({"slot": "morning"})
    sent = [d["message"] for s, d in app.calls if s == "notify/send_message"]
    assert sent == ["MORNING\n📌 להיום\n• Dana — hat"] and not [s for s, _ in app.calls if s == "ai_task/generate_data"]
    app2 = _summary_live(tempfile.mkdtemp(), {}, [], [])               # nothing today, nothing new: silence
    app2.run_summary({"slot": "morning"})
    assert not [s for s, _ in app2.calls if s == "notify/send_message"]


def test_summary_failure_is_reported_and_queue_kept():
    tmp = tempfile.mkdtemp()
    app = _summary_live(tmp, {}, [], [_q()])
    app.run_summary({"slot": "evening"})
    sent = [d["message"] for s, d in app.calls if s == "notify/send_message"]
    assert len(sent) == 1 and sent[0].startswith("⚠️") and "1" in sent[0]
    assert open(os.path.join(tmp, "queue.jsonl"), encoding="utf-8").read().strip() != ""



def _morning_live(tmp, plans, tasks=()):
    json.dump(plans, open(os.path.join(tmp, "weekly_plans.json"), "w", encoding="utf-8"), ensure_ascii=False)
    services = {"todo/get_items": lambda d: {"response": {"todo.t": {"items": list(tasks)}}}}
    args = {"shadow": False, "data_dir": tmp, "tasks_todo": "todo.t", "school_calendar_sensor": "sensor.school",
            "issur_melacha_sensor": "binary_sensor.sb", "notify_entity": "notify.x", "whatsapp_to": "972500000000@c.us",
            "waha_url": "http://waha", "waha_header_file": "/dev/null",
            "kids": [{"key": "kid_a", "name": "Dana", "school": True, "pointer": "input_text.p", "sensor": "sensor.plan"}]}
    return fake_hass.make("wa_morning", "WaMorning", args, {}, services)


def _capture_whatsapp():
    """Replace wa_send.whatsapp_send for one test. The caller MUST call restore(): the self-tests run inside
    AppDaemon, where this module is shared with the live apps."""
    import wa_send
    sent, original = [], wa_send.whatsapp_send
    wa_send.whatsapp_send = lambda cfg, mode, **kw: sent.append((mode, kw)) or True
    return sent, lambda: setattr(wa_send, "whatsapp_send", original)


def test_evening_schedule_sends_tomorrow_to_whatsapp_only():
    import datetime as _dt
    sent, restore = _capture_whatsapp()
    try:
        tmo = _dt.date.today() + _dt.timedelta(days=1)
        tmp = tempfile.mkdtemp()
        entry = {"date": tmo.isoformat(), "lessons": ["L1"], "bring": ["ruler"], "notes": [], "hours": ""}
        app = _morning_live(tmp, {"kid_a": {"file": "wa/x.pdf", "start": "2020-01-01", "end": "2099-01-01", "days": [entry]}})
        app.run_evening_schedule({})
        if tmo.weekday() == 5:
            assert sent == []                                  # tomorrow is Saturday: nothing
        else:
            assert len(sent) == 1 and sent[0][0] == "text" and "מחר" in sent[0][1]["text"] and "ruler" in sent[0][1]["text"]
        assert not [s for s, _ in app.calls if s == "notify/send_message"]   # evening goes to WhatsApp only
    finally:
        restore()


def test_morning_0715_is_telegram_only():
    import datetime as _dt
    sent, restore = _capture_whatsapp()
    try:
        today = _dt.date.today()
        tmp = tempfile.mkdtemp()
        entry = {"date": today.isoformat(), "lessons": ["L1"], "bring": [], "notes": [], "hours": ""}
        app = _morning_live(tmp, {"kid_a": {"file": "wa/x.pdf", "start": "2020-01-01", "end": "2099-01-01", "days": [entry]}})
        app.states.update({"input_text.p": {"state": "wa/x.pdf|2020-01-01|2099-01-01|t"}})
        app.run_daily_schedule({})
        assert sent == []                                      # nothing goes to WhatsApp at 07:15 any more
    finally:
        restore()


def test_selftests_leave_whatsapp_send_intact():
    import wa_send
    sent, restore = _capture_whatsapp()
    restore()
    assert wa_send.whatsapp_send.__module__ == "wa_send"



def test_alert_goes_to_whatsapp_when_enabled():
    sent, restore = _capture_whatsapp()
    try:
        tmp = tempfile.mkdtemp()
        args = {"shadow": False, "groups": GROUPS, "groups_helper": "input_text.g", "alert_names": ["Dana"], "data_dir": tmp,
                "media_root": "wa_test_media", "waha_url": "http://waha", "waha_header_file": "/dev/null", "ai_task_entity": "ai_task.x",
                "alert_group_labels": {"111": "Class A"}, "notify_entity": "notify.x", "alerts_to_whatsapp": True, "whatsapp_to": "972500000000@c.us"}
        app = fake_hass.make("wa_ingest", "WaIngest", args, {"input_text.g": {"state": ""}}, {})
        import wa_send
        wa_send.send_seen = lambda cfg, chat: 200
        app._seen_and_alert("111@g.us", {"title": "⚠️ דחוף — Class A", "text": "T: no school"})
        assert sent == [("text", {"text": "⚠️ דחוף — Class A\nT: no school", "log": app.log})] or sent[0][1]["text"].startswith("⚠️")
        assert [s for s, _ in app.calls if s == "notify/send_message"]
    finally:
        restore()
