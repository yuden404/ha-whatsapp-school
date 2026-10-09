# Family WhatsApp assistant — implementation plan

Status: plan, approved by the owner on 2026-10-08. To be implemented in `apps/whatsapp/` (AppDaemon), following the existing conventions: pure logic in `wa_core.py`, apps thin, fake-Hass tests, no personal data in the repo, English prompts in `prompts/`, strings in `locales/`.

## 1. Goal

The dedicated WhatsApp number already reads the kids' school and kindergarten groups and sends summaries to the family contact. Now the two parents can **write to that number** and get answers:

| Kind | Example (Hebrew, as users write) | Answered from |
|---|---|---|
| School knowledge | "באיזו קבוצה דנה בגינה הלימודית?" · "מה הקוד לשער בגן של רון?" · "מי בוועד של נועם?" | facts memory + message archive |
| Current state | "מה המערכת מחר?" · "מה המשימות הפתוחות?" · "מה היה היום בקבוצות?" | weekly plans, task list, calendar, today's queue/archive |
| Remember | "תזכרי שהקוד לשער של רון הוא 1234" | writes a fact |
| Home control | "תכבי את האור במטבח" · "הדלת נעולה?" | Home Assistant conversation agent, same as the Telegram bot |

Non-goals: answering anyone except the two allowed numbers; writing to groups (never); a vector database.

## 2. Why not RAG

Volume is ~40 messages/day (~15k/year, mostly one or two sentences). Two months of messages are ~100k tokens and fit a single Gemini context. The hard questions are **durable facts stated once** (a gate code, committee names), which semantic search in Hebrew retrieves unreliably. So: an explicit **facts memory** extracted by the model at summary time, plus a **recent-messages window**, plus **structured state** the apps already keep. One model call per question, ~30k tokens, under 10 agorot.

## 3. Architecture

```
WAHA webhook ──► HA automation (forwarder) ──► event wa_shadow_webhook
                                                 │
                 ┌───────────────────────────────┴──────────────────────────────┐
                 ▼                                                              ▼
        WaIngest (groups, unchanged)                                 WaAssistant (NEW)
        + append to archive/YYYY-MM.jsonl (NEW)                      only chat_id in allowed_contacts (2 numbers)
                 │                                                              │
        WaSummary (unchanged + facts extraction NEW)                            │ classify + answer
        writes data/facts.json                                                  ▼
                                                                  reply via tools/waha_send.py (text only, 1:1)
```

### 3.1 Data (all under `data_dir`, never in the repo)
- `archive/YYYY-MM.jsonl` — every accepted group message, forever. Same record as the queue (`id, ts, chat, sender, item, caption, ftext, media, mime, mclass, kind`). Written by `WaIngest` right after the queue append. Size: ~2 MB/year.
- `facts.json` — `{"facts": [{"id", "child", "key", "value", "source_group", "msg_id", "first_seen", "last_seen", "superseded_by"}]}`. `key` is a short normalised Hebrew noun phrase chosen by the model (e.g. "קוד שער", "חברי ועד", "קבוצה בגינה הלימודית"). One active fact per (child, key); a new value supersedes the old one (keep the old row with `superseded_by`).
- `assistant/YYYY-MM.jsonl` — question/answer log (for debugging and the daily cost line). Rotated like the archive.

### 3.2 Facts extraction (in `WaSummary`, both slots)
Add to `prompts/summary.md` and `prompts/summary.schema.json` a fourth list:
`facts: [{child, key, value, evidence}]` — "durable facts a parent will need again later: codes, committee members, group assignments, opening hours, fees, contact persons, recurring days (e.g. sport on Tuesdays). Not one-off tasks or events." Validation in `wa_core.merge_facts(existing, new, today)`: trim, drop empty, same (child,key) → supersede when value differs, else bump `last_seen`. Hard cap 500 active facts.

### 3.3 Assistant (`wa_assistant.py`, class `WaAssistant`)
Trigger: `wa_shadow_webhook` events where `event == "message"`, `payload.from` ∈ `allowed_contacts`, not `fromMe`, type `chat` or `ptt` (voice note → transcribe with the existing audio prompt first).

Flow per message:
1. **Rate limit**: max `rate_limit_per_hour` (default 20) per contact; beyond that reply once with a short "רגע, הרבה שאלות" and drop.
2. **Context packet** (`wa_core.build_context(...)`, pure):
   - active facts (all; cap 500 lines),
   - weekly plans of all kids: today, tomorrow, and the rest of the week (from `weekly_plans.json`),
   - open tasks (todo list, `needs_action`, next 14 days),
   - parent events (calendar `get_events`, next 30 days),
   - archive window: last `context_days` (default 14) days of messages, newest last, truncated to `context_max_chars` (default 60k) from the oldest side,
   - today's date and weekday, the group map, the children's names.
3. **One structured model call** (`prompts/assistant.md` + `assistant.schema.json`):
   ```
   route: school | home | remember | smalltalk
   answer: string            # Hebrew, short, WhatsApp-friendly, no markdown
   fact:   {child, key, value}   # only when route == remember
   confidence: high | medium | low
   ```
   Rules in the prompt: answer only from the packet; if the information is not there say so plainly ("אין לי את זה בהודעות") and, if useful, where it might be (which group); never invent codes, names or dates; quote the date/sender of the source message for facts; for "tomorrow" use the packet's date arithmetic (already computed), not your own.
4. **Dispatch by route**
   - `school` / `smalltalk`: send `answer`.
   - `remember`: `merge_facts`, then confirm ("שמרתי: …"). Facts added by chat get `source_group: "chat"`.
   - `home`: call `conversation/process` with `agent_id = home_agent` (the Telegram bot's agent, same exposed entities) and `text = original message`; send the agent's `speech.plain.speech`. If the agent returns an error, send "הבית לא ענה" (never pretend the action happened).
5. **Reply** via `wa_send.whatsapp_send(cfg, "text", …)` to the *sender* (not a fixed contact). Log to `assistant/`.

### 3.4 Historical import (one-off, `tools/import_history.py`)
WAHA returns old messages per chat (`GET /api/{session}/chats/{chatId}/messages?limit=…&downloadMedia=false`). The tool: for every monitored group, page back to `--since` (default 90 days), build archive records (text only, no media download), write to `archive/`, then run the facts extraction prompt over the imported text in monthly chunks (one model call per chunk, ~10 calls). Runs inside the AppDaemon container (`python3 tools/import_history.py --since 2026-07-01`) and is idempotent (skips ids already archived).

## 4. Guardrails

- **Who**: `allowed_contacts` list in `apps.yaml` (two chat ids). Everything else is dropped before any model call. Groups are never answered.
- **What the model may do**: only produce text. All side effects (fact writes, home actions, sending) are code paths gated by the `route` value and by the allowlist.
- **Home control**: identical permissions to the Telegram bot (same agent id, same exposed entities). No new exposures as part of this work.
- **Honesty**: "unknown" is a first-class answer; a test asserts the prompt contains the rule and that a packet without the answer yields no invented value (fake model).
- **Privacy**: facts and archive stay in `data_dir`; the privacy scanner already blocks group ids and names from the repo. `allowed_contacts` are personal values and are covered by the dynamic scan.
- **Cost**: one call per question + one small call per summary slot for facts; archive import ~10 calls once. A daily cost line (count of assistant calls) goes into the existing Telegram failure/report channel only if above `daily_call_alert` (default 100).
- **Ban risk**: replies go only to a saved contact who wrote first; random 2–6 s delay before replying; no more than `rate_limit_per_hour` messages.

## 5. Configuration (`apps.yaml`, personal)

```yaml
wa_assistant:
  module: wa_assistant
  class: WaAssistant
  language: he
  data_dir: /homeassistant/appdaemon/data
  allowed_contacts: ["<parent1>@c.us", "<parent2>@c.us"]
  home_agent: conversation.<your_agent>
  ai_task_entity: ai_task.<yours>
  tasks_todo: todo.<tasks>
  calendars_helper: input_text.<calendars>
  waha_url: http://<waha-host>:3000
  waha_header_file: /homeassistant/.waha_curl_header
  groups: {...}            # same map as wa_ingest
  group_labels: {...}      # same as wa_summary
  child_names: {kid_a: "..."}
  context_days: 14
  context_max_chars: 60000
  rate_limit_per_hour: 20
```
`apps.yaml.example` gets the same block with placeholders; `tools/privacy_scan.py` needs no change (`@c.us` ids are already caught).

## 6. Code changes, by file

| File | Change |
|---|---|
| `wa_core.py` | `merge_facts()`, `build_context()`, `format_plans_for_context()`, `format_tasks()`, `truncate_from_start()`; all pure, tested |
| `wa_ingest.py` | append to `archive/` after the queue append (one line) |
| `wa_summary.py` | `facts` in structure; after a successful run `merge_facts` → `facts.json` |
| `wa_assistant.py` | new app (≈200 lines): allowlist, rate limit, packet, model call, dispatch, reply, log |
| `prompts/assistant.md`, `assistant.schema.json` | new |
| `prompts/summary.md`, `summary.schema.json` | facts list |
| `locales/he.json`, `en.json` | assistant strings (unknown, saved, home-failed, rate-limited) |
| `tools/import_history.py` | new, one-off |
| `wa_tests/test_wa_core.py` | merge_facts (supersede, cap, trim), build_context (window, truncation from the oldest side, tomorrow date), format helpers |
| `wa_tests/test_apps.py` | assistant: drops non-allowed senders; school route replies; remember writes a fact and confirms; home route calls `conversation/process` with the right agent and relays speech; unknown stays unknown; rate limit |
| `wa_selftest.py` | nothing new required (unit tests are discovered automatically) |
| `README.md` | "Chat with the number" section + privacy notes |

## 7. Rollout

1. **Day 0**: archive + facts extraction. Nothing user-visible; data starts accumulating. Run `import_history.py --since 90d` once and review `facts.json` by hand (expect some noise the first time; tune the facts rule in the prompt).
2. **Day 1**: assistant in **dry-run** (`reply: false`): questions are answered into the log only. Owner asks 10–20 real questions, reads the log, tunes the prompt.
3. **Day 2**: `reply: true` for the owner's number only. Then add the second contact.
4. **Home route** is enabled last, with `home_agent` set; until then the route replies "שליטה בבית עוד לא פעילה כאן".

## 8. Decisions (owner, 2026-10-08)

1. **Answer style**: short plain sentences; bullets only when the answer is a list (several tasks, a day's schedule).
2. **Archive retention**: keep forever, but watch the size: add a monthly disk check; if the archive passes 200 MB, alert and start rolling old months into yearly files.
3. **Shared memory**: a fact remembered by either parent is visible to both; the assistant says who taught it and when if asked.
