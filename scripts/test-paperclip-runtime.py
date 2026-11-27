#!/usr/bin/env python3
"""Opt-in native component probe; uses only disposable, credential-free state.

Does not build/qualify the Docker image or automate a human account. Both test
listeners are forced to loopback, including the authenticated server which is
normally bound to the companion container's interfaces behind Docker's loopback
publication. No provider CLI may run: a fixture sentinel catches any invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "modules/common/templates/runtime/workspace-paperclip.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--paperclip", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("AGENT_STACK_PAPERCLIP_RUNTIME_TESTS") != "1":
        parser.error("Set AGENT_STACK_PAPERCLIP_RUNTIME_TESTS=1 for this temporary local-server test")
    node, package = args.node.resolve(strict=True), args.paperclip.resolve(strict=True)
    os.umask(0o077)
    work_parent = Path("/tmp/agent-stack-tests")
    work_parent.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="paperclip-runtime.", dir=work_parent))
    home = root / "home"
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "node").symlink_to(node)
    calls = root / "codex-invocations"
    (bin_dir / "codex").write_text(f"#!/bin/sh\nprintf 'unexpected-codex-invocation\\n' >> '{calls}'\nexit 42\n")
    (bin_dir / "codex").chmod(0o700)
    spec = importlib.util.spec_from_file_location("workspace_paperclip", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sourceSha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "environment": {"architecture": platform.machine(), "os": platform.platform(), "libc": platform.libc_ver(), "dockerImageQualified": False, "transport": "forced host loopback"},
        "checks": [], "codingCredentials": "none", "realAgentsActivated": False,
    }

    def passed(name: str, **evidence):
        results["checks"].append({"name": name, "status": "passed", **evidence})

    def request(port: int, method: str, path: str):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"{}" if method == "POST" else None, headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def loopback_listener(port: int):
        # Check the actual IPv4 socket table, not just our intended config.
        listeners = []
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            cells = line.split()
            address, local_port = cells[1].split(":")
            if int(local_port, 16) == port and cells[3] == "0A":
                listeners.append(address)
        assert listeners and set(listeners) == {"0100007F"}, f"Port {port} is not exclusively loopback"

    class Probe(module.Runtime):
        def __init__(self, fail=False):
            super().__init__(home, (str(package),))
            self.fail = fail
            self.starts = []

        def fresh_config(self, *, bootstrap):
            config = super().fresh_config(bootstrap=bootstrap)
            # The native probe has no Docker network namespace. Never inherit
            # the production container's bind-all transport on the real host.
            config["server"].update({"bind": "loopback", "host": "127.0.0.1"})
            return config

        def start(self):
            self.starts.append(module.read_json(self.config_path)["server"]["deploymentMode"])
            return super().start()

        def stored_state(self, state):
            # Read only this disposable agent's non-secret configuration and
            # counters; never query existing user data or credential material.
            script = """const path = require('node:path');
const postgres = require(require.resolve('postgres', {paths:[path.dirname(process.argv[3])]}));
(async () => {
 const sql = postgres('postgres://paperclip:paperclip@127.0.0.1:54329/paperclip');
 try {
  const rows = await sql.unsafe('SELECT adapter_config,status,lifecycle_state,lifecycle_holds,runtime_config FROM agents WHERE id=$1', [process.argv[1]]);
  if (rows.length !== 1) throw Error('starter agent missing');
  const row = rows[0], env = row.adapter_config.env, binding = env.CODEX_HOME;
  const value = typeof binding === 'string' ? binding : binding.value;
  if (value !== process.argv[2] || Object.keys(env).join(',') !== 'CODEX_HOME') throw Error('stored coding home mismatch');
  if (row.status !== 'paused' || row.lifecycle_state !== 'paused' || !row.lifecycle_holds.includes('setup')) throw Error('starter is not setup-held');
  if (row.runtime_config.heartbeat.enabled !== false || row.adapter_config.dangerouslyBypassApprovalsAndSandbox !== false) throw Error('starter configuration changed');
  const counts = await sql.unsafe('SELECT (SELECT count(*) FROM agents)::int AS agents, (SELECT count(*) FROM heartbeat_runs)::int AS runs');
  if (counts[0].agents !== 1 || counts[0].runs !== 0) throw Error('unexpected agents or runs');
  console.log(JSON.stringify({agents:1,runs:0,paused:true,storedHomeMatches:true}));
 } finally { await sql.end(); }
})().catch(error => { console.error(error.message); process.exitCode=1; });"""
            output = subprocess.run([str(node), "-e", script, state["setupId"], str(self.codex_home), str(package)], env=self.environment(), cwd=root, check=True, text=True, capture_output=True)
            return json.loads(output.stdout)

        def seed(self, state):
            super().seed(state)
            loopback_listener(3101)
            passed("paused-seed-and-stored-isolation", **self.stored_state(state))
            for action in ("resume", "wakeup"):
                status, _body = request(3101, "POST", f"/api/agents/{state['setupId']}/{action}")
                assert status == 409, f"Paused {action} returned HTTP {status}"
                passed(f"paused-{action}-denied", http=status)
            time.sleep(6)
            assert self.api("GET", f"/companies/{state['companyId']}/heartbeat-runs") == []
            assert not calls.exists(), "A coding executable was unexpectedly invoked"
            passed("no-runs-or-coding-process", observationSeconds=6)
            if self.fail:
                self.fail = False
                raise module.SetupError("fixture interruption after committed seed")

    def wait_health(runtime):
        for _ in range(180):
            if runtime.child.poll() is not None:
                raise RuntimeError("Paperclip exited; inspect the private fixture launch log")
            try:
                runtime.health()
                loopback_listener(3100)
                return
            except (OSError, ValueError, module.SetupError):
                time.sleep(1)
        raise RuntimeError("Paperclip health timed out")

    runtime, restarted = Probe(fail=True), None
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt("local probe interrupted")
    signal.signal(signal.SIGTERM, interrupt)
    try:
        # Refuse collisions. Never stop or adopt another service for a probe.
        for port in (3100, 3101, 54329):
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", port))
        env = runtime.environment()
        node_version = subprocess.check_output([str(node), "--version"], env=env, text=True).strip()
        package_version = subprocess.check_output([str(package), "--version"], env=env, text=True).strip()
        lock = json.loads((SOURCE.parent / "workspace-paperclip-package-lock.json").read_text())
        assert package_version == lock["packages"][""]["dependencies"]["paperclipai"], "Paperclip does not match the pinned release"
        results["environment"].update({"node": node_version, "paperclip": package_version})
        try:
            runtime.initialize()
        except module.SetupError as error:
            if str(error) != "fixture interruption after committed seed":
                raise
        before = module.read_json(runtime.state_path)
        assert before["phase"] == "initializing"
        after = runtime.initialize()
        assert before["setupId"] == after["setupId"] and before["companyId"] == after["companyId"]
        assert after["phase"] == "complete"
        passed("committed-seed-retry-same-identities")
        runtime.start()
        wait_health(runtime)
        status, body = request(3100, "GET", "/")
        assert status == 200 and b"<!doctype html" in body.lower(), "Bundled UI not served"
        passed("bundled-ui-and-authenticated-health", uiHttp=status, unauthenticatedCompaniesHttp=request(3100, "GET", "/api/companies")[0])
        assert re.search(r"/board-claim/[0-9a-f]+\?code=", runtime.log_path.read_text()), "Missing first-owner claim"
        passed("private-first-owner-claim-present")
        runtime.stop()
        fingerprints = {name: hashlib.sha256((runtime.instance / name).read_bytes()).hexdigest() for name in (".env", "secrets/master.key")}
        restarted = Probe()
        assert restarted.initialize() == after and restarted.starts == []
        restarted.start()
        wait_health(restarted)
        assert restarted.starts == ["authenticated"]
        assert fingerprints == {name: hashlib.sha256((runtime.instance / name).read_bytes()).hexdigest() for name in (".env", "secrets/master.key")}
        assert not calls.exists()
        passed("authenticated-restart-without-reseed-or-key-rotation", **restarted.stored_state(after))
        results["result"] = "passed"
    except BaseException as error:
        results["result"] = "blocked" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
        results["error"] = str(error)
        raise
    finally:
        runtime.stop()
        if restarted:
            restarted.stop()
        (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps({"result": results.get("result"), "checks": len(results["checks"]), "evidence": str(root / "results.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
