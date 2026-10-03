# ha-whatsapp-school

Turns noisy WhatsApp school and kindergarten groups into something a parent can actually use:
one daily summary of what each kid needs, the weekly class schedule read from the teacher's PDF,
a "what's in the bag today" message every morning, form links as tasks, and parent events in a calendar.

Runs at home on [Home Assistant](https://www.home-assistant.io/) with [AppDaemon](https://appdaemon.readthedocs.io/),
[WAHA](https://waha.devlike.pro/) for WhatsApp and Google Gemini (via Home Assistant's `ai_task`) for understanding messages and documents.

> Status: being migrated from Home Assistant YAML/Jinja to Python. New parts run in **shadow mode**
> next to the existing pipeline and are compared before they take over.

## Design

- **The LLM is a component, not the orchestrator.** AppDaemon owns the flow (queue, schedule, delivery,
  failure handling). Gemini gets small, well defined tasks with structured output.
- **One model call per file.** A single structured call returns the full text, whether it is a weekly
  schedule, the date range and the per-day breakdown. Summaries reuse the stored text.
- **Never trust model output blindly.** Form links must appear verbatim in the source message,
  parent events need a quote as evidence, dates and times are validated.
- **Failures are loud.** Self-tests run after every code change and after every restart.

## Layout

| Path | What |
|---|---|
| `wa_core.py` | Pure logic, no HA imports: filtering, dedupe, dates, morning rules, validation |
| `wa_ingest.py` | Webhook to filter, media download, file read, weekly plan (shadow capable) |
| `wa_selftest.py` | Unit tests, parity tests against the old Jinja implementation, live integration checks |
| `wa_publish.py` | Publishes this folder to GitHub behind the privacy gate |
| `prompts/` | Model instructions and output schemas |
| `wa_tests/` | Unit tests (synthetic data only) |
| `tools/privacy_scan.py` | Blocks any personal value from being committed |

## Setup

1. Install the AppDaemon add-on and point its `apps` folder at a directory you control.
2. Copy `apps.example.yaml` to `apps/apps.yaml` **outside this folder** and fill in your own
   group ids, names and entities. Copy `appdaemon.example.yaml` likewise.
3. WAHA: put a one-line file `X-Api-Key: <key>` somewhere outside the repo and set `waha_header_file`.
4. Gemini: add the Google Generative AI integration in Home Assistant and set `ai_task_entity`.
5. Run the unit tests locally: `pip install -r requirements-dev.txt && pytest wa_tests`.

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
