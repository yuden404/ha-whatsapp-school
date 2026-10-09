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


class JsonFile:
    """A small JSON document (facts, plans) with a file lock and atomic writes."""

    def __init__(self, path: str, default):
        self.path, self.default = path, default
        os.makedirs(os.path.dirname(path), exist_ok=True)

    @contextmanager
    def _locked(self):
        with open(self.path + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        if not os.path.exists(self.path):
            return json.loads(json.dumps(self.default))
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def read(self):
        with self._locked():
            return self._read()

    def update(self, fn):
        """fn(data) -> new data, applied under the lock."""
        with self._locked():
            data = fn(self._read())
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return data


def archive_path(data_dir: str, ts: int | float | None = None) -> str:
    """data_dir/archive/YYYY-MM.jsonl for a unix timestamp (now when missing)."""
    from datetime import datetime
    d = datetime.fromtimestamp(ts) if ts else datetime.now()
    return os.path.join(data_dir, "archive", f"{d:%Y-%m}.jsonl")
