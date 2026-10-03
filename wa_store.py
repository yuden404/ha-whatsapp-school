"""Tiny JSONL queue shared by the apps (ingest appends, summary consumes, night reads).

A file lock makes append/consume safe against each other: AppDaemon runs apps on separate
threads, so without it a summary rewrite could drop a message that arrived mid-write.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager


class JsonlQueue:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    @contextmanager
    def _locked(self):
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append(self, item: dict) -> None:
        with self._locked(), open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with self._locked(), open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def drop(self, ids: set[str]) -> None:
        """Remove consumed items atomically; anything appended meanwhile is kept."""
        with self._locked():
            rest = []
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    rest = [json.loads(line) for line in f if line.strip() and json.loads(line).get("id") not in ids]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(q, ensure_ascii=False) + "\n" for q in rest)
            os.replace(tmp, self.path)
