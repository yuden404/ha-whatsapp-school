"""AppDaemon app: publish this folder to its public GitHub repo, behind the privacy gate.

Triggered only by event wa_repo_publish {mode: "dry_run" | "push", message: "..."}.
dry_run: privacy scan + list of changed files, nothing leaves the machine.
push:    privacy scan must be clean, then commit + push with a repo-only deploy key.
Result goes to sensor.wa_repo_publish.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import appdaemon.plugins.hass.hassapi as hass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import privacy_scan  # noqa: E402


class WaPublish(hass.Hass):
    def initialize(self):
        importlib.reload(privacy_scan)
        self.cfg = self.args
        self.listen_event(self.on_publish, "wa_repo_publish")

    def _git(self, *args, check=True):
        env = dict(os.environ, GIT_SSH_COMMAND=(
            f"ssh -i {self.cfg['deploy_key']} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={os.path.dirname(self.cfg['deploy_key'])}/known_hosts"))
        r = subprocess.run(["git", *args], cwd=HERE, env=env, capture_output=True, text=True, timeout=120, check=False)
        if check and r.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return r.stdout.strip()

    def _report(self, state, **attrs):
        self.set_state("sensor.wa_repo_publish", state=state, attributes=attrs, replace=True)
        self.log(f"publish {state}: {attrs}")

    def on_publish(self, _event, data, _kwargs):
        mode = data.get("mode", "dry_run")
        try:
            hits = privacy_scan.scan(HERE, self.cfg["apps_yaml"])
            if hits:
                return self._report("blocked", hits=hits[:50])
            if not os.path.isdir(os.path.join(HERE, ".git")):
                self._git("init", "-b", "main")
                self._git("remote", "add", "origin", self.cfg["remote"])
            self._git("remote", "set-url", "origin", self.cfg["remote"])  # always follow apps.yaml
            self._git("config", "user.name", self.cfg["git_name"])
            self._git("config", "user.email", self.cfg["git_email"])
            self._git("add", "-A")
            files = self._git("status", "--porcelain").splitlines()
            if mode != "push":
                return self._report("dry_run_ok", files=files)
            if files:
                self._git("commit", "-m", data.get("message") or "update")
            out = self._git("push", "-u", "origin", "main")
            self._report("pushed", files=files, head=self._git("rev-parse", "--short", "HEAD"), output=out[-300:])
        except Exception as e:  # noqa: BLE001
            remote = self._git("remote", "-v", check=False) if os.path.isdir(os.path.join(HERE, ".git")) else ""
            self._report("error", error=f"{type(e).__name__}: {e}"[:400], remote=remote[:200])
