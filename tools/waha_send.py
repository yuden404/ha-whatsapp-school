#!/usr/bin/env python3
"""Send a WhatsApp text or file through WAHA. Used by Home Assistant shell_command (and later by the apps).

usage: waha_send.py <waha_url> <header_file> text <chat_id> <text_base64>
       waha_send.py <waha_url> <header_file> file <chat_id> <path> [caption_base64]
The text travels base64-encoded so quotes and newlines survive the shell. Exit 0 on success.
Only ever used for one-to-one messages to a known contact, never to groups.
"""
import base64
import json
import mimetypes
import os
import sys
import urllib.request


def main(argv):
    url, header_file, mode, chat = argv[1:5]
    with open(header_file, encoding="utf-8") as f:
        k, v = f.read().strip().split(":", 1)
    headers = {k.strip(): v.strip(), "Content-Type": "application/json"}
    if mode == "text":
        body = {"session": "default", "chatId": chat, "text": base64.b64decode(argv[5]).decode("utf-8")}
        endpoint = "/api/sendText"
    elif mode == "file":
        path = argv[5]
        caption = base64.b64decode(argv[6]).decode("utf-8") if len(argv) > 6 and argv[6] else ""
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        body = {"session": "default", "chatId": chat, "caption": caption,
                "file": {"mimetype": mimetypes.guess_type(path)[0] or "application/octet-stream",
                         "filename": os.path.basename(path), "data": data}}
        endpoint = "/api/sendFile"
    else:
        raise SystemExit(f"unknown mode {mode}")
    req = urllib.request.Request(url.rstrip("/") + endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.status)


if __name__ == "__main__":
    main(sys.argv)
