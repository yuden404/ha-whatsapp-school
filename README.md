# ha-whatsapp-school

Turns noisy WhatsApp school and kindergarten groups into something a parent can actually use:
one daily summary of what each kid needs, the weekly class schedule read from the teacher's PDF,
a "what's in the bag today" message every morning, form links as tasks, and parent events in a calendar.

Runs at home on [Home Assistant](https://www.home-assistant.io/) with [AppDaemon](https://appdaemon.readthedocs.io/),
[WAHA](https://waha.devlike.pro/) for WhatsApp and Google Gemini (via Home Assistant's `ai_task`) for understanding messages and documents.

> Status: live since 2026-10-06. The YAML/Jinja pipeline it replaced ran in parallel for three days first, compared message by message.

## Design

- **The LLM is a component, not the orchestrator.** AppDaemon owns the flow (queue, schedule, delivery,
  failure handling). Gemini gets small, well defined tasks with structured output.
- **One model call per file.** A single structured call returns the full text, whether it is a weekly
  schedule, the date range and the per-day breakdown. Summaries reuse the stored text.
- **Never trust model output blindly.** Form links must appear verbatim in the source message,
  parent events need a quote as evidence, dates and times are validated.
- **Failures are loud.** Self-tests run after every code change and after every restart.

## What runs when

| When | What | Who gets it |
|---|---|---|
| every message | filter, download attachment, read documents and captioned images, transcribe voice notes, store a weekly schedule | — |
| 06:00–23:00, 30–120 s after a message | WhatsApp read receipt; alert for urgent / sign-up / emergency / child mentioned | Telegram + WhatsApp contact |
| 07:00 | overnight digest of alert-worthy messages (read receipts first) | Telegram + WhatsApp contact |
| 07:15 | today's schedule per school child | Telegram |
| 07:30 | one morning message: due today + new overnight | Telegram + WhatsApp contact |
| 21:00 | evening summary (tasks, forms, parent events → task list and calendar) + PDFs | Telegram + WhatsApp contact |
| 21:05 | tomorrow's schedule | WhatsApp contact |

Media: PDFs always, images only with a caption or from a staff group, voice notes always (transcribed), video never read. A run of photos/videos from one sender becomes one line in the summary. Videos are kept a week, other files three weeks.

Next: a chat assistant on the same number, see [docs/assistant-plan.md](docs/assistant-plan.md).

## Layout

| Path | What |
|---|---|
| `wa_core.py` | Pure logic, no HA imports: filtering, dedupe, dates, morning rules, validation |
| `wa_ingest.py` | Webhook → filter, media, document and voice reading, weekly plan, read receipts, alerts |
| `wa_summary.py` | Evening and morning summaries; tasks, forms, parent events; delivery |
| `wa_morning.py` | Schedules (07:15 / 21:05), night digest |
| `wa_send.py`, `tools/waha_send.py` | Outbound WhatsApp (one-to-one only), read receipts |
| `wa_store.py` | Locked JSONL queue shared by the apps |
| `docs/` | Design notes and plans |
| `wa_selftest.py` | Unit tests, parity tests against the old Jinja implementation, live integration checks |
| `wa_publish.py` | Publishes this folder to GitHub behind the privacy gate |
| `prompts/*.md` | Model instructions (English, structured: role, context, input, rules, output). Placeholders like `{TODAY}` are filled at runtime from your config, so no personal data lives in them |
| `prompts/*.schema.json` | Output schemas for Home Assistant `ai_task` structured output |
| `locales/` | Every user-facing string, per language (`he.json`, `en.json`). Pick with `language:` in apps.yaml |
| `wa_tests/` | Unit tests, app tests on a fake Home Assistant (`fake_hass.py`), synthetic data only |
| `tools/privacy_scan.py` | Blocks any personal value from being committed |

## Setup

1. Install the AppDaemon add-on and point its `apps` folder at a directory you control.
2. Copy `apps.yaml.example` to `apps/apps.yaml` **outside this folder** and fill in your own
   group ids, names and entities (the example files are not named .yaml on purpose:
   AppDaemon loads every .yaml in its apps folder). Copy `appdaemon.yaml.example` likewise.
3. WAHA: put a one-line file `X-Api-Key: <key>` somewhere outside the repo and set `waha_header_file`.
4. Gemini: add the Google Generative AI integration in Home Assistant and set `ai_task_entity`.
5. Restart AppDaemon after editing `apps.yaml`: hot reload of a changed app config can crash in AppDaemon 4.5.
6. Run the unit tests locally: `pip install -r requirements-dev.txt && pytest wa_tests`.

## Privacy

Nothing personal is stored in this repository: no group ids, names, phone numbers, addresses,
entity ids or keys. All of it lives in your local `apps.yaml`. `tools/privacy_scan.py` checks both
generic patterns (WhatsApp ids, private IPs, API keys, phone numbers, entity ids) and every value from
your `apps.yaml`, and `wa_publish` refuses to push if anything is found.

## WhatsApp terms of service

WAHA drives WhatsApp Web, which is not an official API and is against WhatsApp's terms of service.
The number can be banned. Use a separate, cheap number added to the groups, and keep the integration
read-only: this project never sends messages to the groups.

## License

MIT
