"""Outbound WhatsApp (one-to-one, to the configured family contact only) and WAHA helpers."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def whatsapp_send(cfg: dict, mode: str, text: str = "", path: str = "", caption: str = "", log=None) -> bool:
    """mode 'text' | 'file' via tools/waha_send.py. Returns False (and logs) on failure, never raises."""
    chat = cfg.get("whatsapp_to")
    if not chat:
        return False
    b64 = lambda t: base64.b64encode(t.encode("utf-8")).decode("ascii")  # noqa: E731
    args = [sys.executable, os.path.join(HERE, "tools", "waha_send.py"), cfg["waha_url"], cfg["waha_header_file"], mode, chat]
    args += [b64(text)] if mode == "text" else [path, b64(caption)]
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=90)
        return True
    except Exception as ex:  # noqa: BLE001
        if log:
            log(f"whatsapp send failed: {ex}", level="WARNING")
        return False


def waha_headers(cfg: dict) -> dict:
    with open(cfg["waha_header_file"], encoding="utf-8") as f:
        k, v = f.read().strip().split(":", 1)
    return {k.strip(): v.strip()}


def send_seen(cfg: dict, chat: str) -> int:
    req = urllib.request.Request(cfg["waha_url"].rstrip("/") + "/api/sendSeen",
                                 data=json.dumps({"session": "default", "chatId": chat}).encode("utf-8"),
                                 headers={**waha_headers(cfg), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status
