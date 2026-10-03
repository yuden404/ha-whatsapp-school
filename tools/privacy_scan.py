"""Privacy gate for the public repo. Exit code 1 (and a list of hits) if anything personal is found.

Two layers:
1. static patterns: WhatsApp ids, private IPs, API keys, phone numbers, HA entity ids
2. dynamic: every personal value from your local apps.yaml (group ids, child keys,
   names, hosts, entity ids), so new personal values are blocked automatically.
Usage: python tools/privacy_scan.py <repo_dir> <path/to/apps.yaml>
"""
from __future__ import annotations

import os
import re
import sys

STATIC = {
    "whatsapp_id": re.compile(r"\b\d{15,}(@g\.us|@c\.us|@lid)?\b"),
    "private_ip": re.compile(r"\b(10|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(\.\d{1,3})?\b"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    "api_key_header": re.compile(r"X-Api-Key:\s*[A-Za-z0-9]{8,}", re.I),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "phone": re.compile(r"(\+?972|\b05\d)[- ]?\d{3}[- ]?\d{4}\b"),
    "ha_entity": re.compile(r"\b(notify|input_text|input_datetime|automation|todo|binary_sensor|calendar|script|ai_task)\.[a-z0-9_]{3,}\b"),
}
ALLOW = {  # generic strings that are fine in code / examples
    "ai_task.generate_data", "notify.send_message", "ai_task.example", "notify.example",
    "todo.example_queue", "input_text.example_groups", "input_datetime.example_last_webhook",
    "binary_sensor.example_waha_running", "automation.example", "calendar.example",
}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
TEXT_EXT = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".toml", ".cfg", ".ini", ""}


def personal_values(apps_yaml: str) -> set[str]:
    import yaml
    with open(apps_yaml, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    vals: set[str] = set()

    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                if key == "groups" or k == "groups":
                    pass
                if key == "groups":
                    vals.add(str(k))
                walk(v, str(k))
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str):
            vals.add(x)

    for app in cfg.values():
        if isinstance(app, dict):
            for k, v in app.items():
                if k in ("module", "class", "remote", "git_name", "git_email"):  # public GitHub identity
                    continue
                walk(v, k)
    out = set()
    for v in vals:
        v = v.strip()
        m = re.match(r"https?://([^/:]+)", v)
        if m:
            out.add(m.group(1))
        elif v.startswith("/"):
            continue  # paths are not personal
        elif len(v) >= 3 and not v.isdigit() or (v.isdigit() and len(v) >= 10):
            out.add(v)
    return out


def scan(repo: str, apps_yaml: str | None) -> list[str]:
    dyn = personal_values(apps_yaml) if apps_yaml and os.path.exists(apps_yaml) else set()
    hits = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.splitext(fn)[1] not in TEXT_EXT:
                continue
            rel = os.path.relpath(p, repo)
            try:
                lines = open(p, encoding="utf-8").read().splitlines()
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(lines, 1):
                for name, rx in STATIC.items():
                    for m in rx.finditer(line):
                        if m.group(0) not in ALLOW:
                            hits.append(f"{rel}:{i} [{name}] {m.group(0)[:40]}")
                for v in dyn:
                    if v in line:
                        hits.append(f"{rel}:{i} [apps.yaml value] {v[:40]}")
    return hits


if __name__ == "__main__":
    h = scan(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print("\n".join(h) if h else "clean")
    sys.exit(1 if h else 0)
