"""OpenCode acceptance: setup, real installer/seed logic, opt-in native loopback CLI.

OPENCODE_TEST_BINARY must name a separately checksum-verified official binary.
Native tests use fresh homes and a scripted localhost server, never a paid model.
"""
import base64
import importlib.util
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_render_tfvars import run_render

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "modules/common/templates/runtime"
BINARY = os.environ.get("OPENCODE_TEST_BINARY", "")


LAUNCHER = ROOT / "modules/common/runtime/workspace_opencode.py"
spec = importlib.util.spec_from_file_location("workspace_opencode", LAUNCHER)
managed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(managed)
KIMI = "huggingface/moonshotai/Kimi-K3:baseten"
MUSE = "huggingface/meta-models/Muse-Spark-Open-816B-A42B:baseten"
DEEPSEEK = "huggingface/deepseek-ai/DeepSeek-V4.1-Flash:baseten"


def seed(home, model=KIMI):
    return subprocess.run([sys.executable, str(LAUNCHER), "--seed", str(home), model], text=True, capture_output=True, timeout=10)


class OpenCodeSetupTest(unittest.TestCase):
    def render(self, success=True, base=None, **overrides):
        data = dict(cloud_provider="digitalocean", do_token="do-fixture-value", tailscale_enabled=False,
                    model_providers_enabled=["google"], gemini_api_key="fixture-google", default_model="google/fixture",
                    fallback_models=[], enabled_services=["workspace"], workspace_password="fixture-password",
                    workspace_harnesses=["codex", "opencode"], workspace_opencode_model=KIMI)
        data.update(overrides)
        return run_render(data, expect_success=success, base_tfvars=base)

    def test_fresh_setup_and_existing_config_merge_keep_independent_models(self):
        for base in (None, 'workspace_username = "operator"\nworkspace_harnesses = ["codex"]\n'):
            result, rendered = self.render(base=base)
            self.assertEqual(result.returncode, 0)
            self.assertIn('workspace_opencode_model = "huggingface/moonshotai/Kimi-K3:baseten"', rendered)
            self.assertIn('default_model = "google/fixture"', rendered)
            self.assertIn('workspace_opencode_release = "1.19.3"', rendered)
            self.assertIn('workspace_claude_enabled = false', rendered)
            self.assertIn('meta_api_key = ""', rendered)
            if base:
                self.assertIn('workspace_username = "operator"', rendered)

    def test_default_codex_and_all_optional_combinations(self):
        for tools in (["codex"], ["codex", "opencode"], ["codex", "claude_code", "opencode"],
                      ["codex", "muse_code", "opencode"], ["codex", "claude_code", "muse_code", "opencode"]):
            with self.subTest(tools=tools):
                self.render(workspace_harnesses=tools, workspace_claude_release="2.1.301", meta_api_key="muse-fixture")
        self.render(workspace_harnesses=["codex"], workspace_opencode_model="")
        for ref in (KIMI, MUSE, DEEPSEEK):
            self.render(workspace_opencode_model=ref)

    def test_missing_workspace_codex_duplicate_unknown_and_missing_model_rejected(self):
        for overrides in (dict(enabled_services=["openclaw"]), dict(workspace_harnesses=["opencode"]),
                          dict(workspace_harnesses=["codex", "opencode", "opencode"]),
                          dict(workspace_harnesses=["codex", "unknown"]), dict(workspace_opencode_model="")):
            with self.subTest(overrides=overrides):
                result, _ = self.render(success=False, **overrides)
                self.assertIn("workspace_", result.stderr)

    def test_rejects_unpinned_v2_and_unsafe_release_or_model(self):
        for release in ("latest", "v1.19.3", "2.0.0", "1.19.3;id", "1.19.3\n"):
            with self.subTest(release=release):
                self.assertIn("workspace_opencode_release", self.render(success=False, workspace_opencode_release=release)[0].stderr)
        for model in ("openai/gpt-4.1", "huggingface/moonshotai/Kimi-K3", "huggingface/moonshotai/Kimi-K3:together", "huggingface/openai/gpt-oss-120b:groq", "huggingface/deepseek-ai/DeepSeek-V4.1-Flash-1017:baseten", "huggingface/deepseek-ai/DeepSeek-V4x1-Flash:baseten", "model-only", "provider/", "https://secret@host/model", "openai/model'", "openai/model\n", "openai/$(id)"):
            with self.subTest(model=model):
                self.assertIn("workspace_opencode_model", self.render(success=False, workspace_opencode_model=model)[0].stderr)


class OpenCodeConfigTest(unittest.TestCase):
    def test_fresh_seed_idempotency_permissions_and_config_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(seed(home).returncode, 0)
            path = home / ".config/opencode/opencode.json"
            expected = managed.initial_config(KIMI)
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
            before = path.read_bytes()
            self.assertEqual(seed(home, MUSE).returncode, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((home / ".local/share/opencode/auth.json").exists())

    def test_existing_json_jsonc_legacy_config_auth_and_sessions_preserved(self):
        for name in ("opencode.json", "opencode.jsonc", "config.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)
                config = home / ".config/opencode"
                config.mkdir(parents=True)
                path = config / name
                path.write_text("// operator fixture, preserve bytes\n{}")
                auth = home / ".local/share/opencode/auth.json"
                auth.parent.mkdir(parents=True)
                auth.write_text('{"fixture":"not-a-real-credential"}')
                auth.chmod(0o600)
                sessions = auth.parent / "opencode.db"
                sessions.write_bytes(b"fixture-session-database")
                snapshot = {p: p.read_bytes() for p in (path, auth, sessions)}
                self.assertEqual(seed(home).returncode, 0)
                for item, content in snapshot.items():
                    self.assertEqual(item.read_bytes(), content)
                self.assertEqual(sorted(p.name for p in config.iterdir()), [name])
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)

    def test_seed_error_has_repair_warning_and_does_not_block_ssh(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".config").write_text("conflict")
            self.assertNotEqual(seed(home).returncode, 0)
        source = (RUNTIME / "workspace-entrypoint.sh.tpl").read_text()
        self.assertIn('if ! runuser -u "$username" -- python3', source)
        self.assertIn("WARNING: could not seed OpenCode config", source)
        self.assertIn("exec /usr/sbin/sshd -D -e", source)

    def test_wrapper_forwards_quoted_arguments_and_native_failure(self):
        source = (RUNTIME / "workspace.Dockerfile.tpl").read_text()
        line = next(line for line in source.splitlines() if ">/usr/local/bin/opencode" in line)
        wrapper = "\n".join(re.findall(r"'([^']*)'", line)[1:]) + "\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "native"
            binary.write_text('import os,sys\nassert os.environ["OPENCODE_DISABLE_AUTOUPDATE"] == "1"\nprint("\\n".join(sys.argv[1:]))\nsys.exit(42)\n')
            binary.chmod(0o755)
            path = root / "opencode"
            path.write_text(wrapper.replace("/usr/local/libexec/agent-stack-opencode.py", str(binary)))
            path.chmod(0o755)
            result = subprocess.run([str(path), "run", "two words", "*"], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 42)
            self.assertEqual(result.stdout.splitlines(), ["run", "two words", "*"])


class OpenCodeInstallerTest(unittest.TestCase):
    def install(self, architecture="aarch64", failure=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tmp").mkdir()
            (root / "usr/local/bin").mkdir(parents=True)
            tools = root / "tools"
            tools.mkdir()
            content = b'#!/bin/sh\nprintf "1.19.3\\n"\n'
            archive = root / "artifact.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("opencode")
                member.size = len(content)
                member.mode = 0o755
                tar.addfile(member, io.BytesIO(content))
            digest = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
            asset = "opencode-linux-" + ("x64-baseline" if architecture == "x86_64" else "arm64") + ".tar.gz"
            release = {"tag_name": "v1.19.3", "draft": False, "prerelease": False,
                       "assets": [{"name": asset, "digest": digest}]}
            if failure == "checksum":
                release["assets"][0]["digest"] = "sha256:" + "0" * 64
            if failure == "missing_digest":
                release["assets"][0]["digest"] = None
            if failure == "wrong_tag":
                release["tag_name"] = "v2.0.0"
            if failure == "duplicate_asset":
                release["assets"] *= 2
            (root / "release.json").write_text(json.dumps(release))
            (tools / "uname").write_text("#!/bin/sh\necho " + architecture + "\n")
            (tools / "curl").write_text("#!/usr/bin/env python3\n" + '''import os,pathlib,shutil,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
with (root/'calls').open('a') as stream: stream.write(' '.join(sys.argv[1:])+'\\n')
if os.environ.get('FIXTURE_FAILURE')=='download': sys.exit(22)
output=sys.argv[sys.argv.index('-o')+1]
shutil.copyfile(root/('release.json' if 'api.github.com' in ' '.join(sys.argv) else 'artifact.tar.gz'),output)
''')
            for tool in tools.iterdir():
                tool.chmod(0o755)
            source = (RUNTIME / "workspace.Dockerfile.tpl").read_text().split("%{ if workspace_opencode_enabled }", 1)[1].split("%{ endif }", 1)[0]
            script = source[source.index("RUN set -eu;") + 4:].replace("\\\n", " ")
            script = script.replace("${workspace_opencode_launcher_base64}", base64.b64encode(LAUNCHER.read_bytes()).decode())
            script = script.replace("/tmp/", str(root / "tmp") + "/").replace("/usr/local", str(root / "usr/local"))
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"], FIXTURE_ROOT=str(root),
                       OPENCODE_RELEASE="1.19.3", FIXTURE_FAILURE=failure or "")
            results = [subprocess.run(["bash", "-c", script], env=env, capture_output=True, timeout=10) for _ in range(2)]
            installed = root / "usr/local/bin/opencode"
            return results, installed.exists(), (root / "calls").read_text() if (root / "calls").exists() else ""

    def test_both_linux_architectures_install_and_reinstall_with_verified_digest(self):
        for arch, platform in (("x86_64", "x64-baseline"), ("aarch64", "arm64")):
            with self.subTest(arch=arch):
                results, installed, calls = self.install(arch)
                for result in results:
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertTrue(installed)
                self.assertIn("opencode-linux-" + platform + ".tar.gz", calls)
                self.assertIn("--proto =https --proto-redir =https", calls)
                self.assertNotIn("latest", calls)

    def test_bad_release_digest_download_or_platform_fails_before_install(self):
        for failure in ("checksum", "missing_digest", "wrong_tag", "duplicate_asset", "download", "architecture"):
            with self.subTest(failure=failure):
                results, installed, _ = self.install("riscv64" if failure == "architecture" else "aarch64", failure)
                self.assertTrue(all(result.returncode != 0 for result in results))
                self.assertFalse(installed)


@unittest.skipUnless(BINARY, "set OPENCODE_TEST_BINARY to a verified official binary for native loopback acceptance")
class OpenCodeNativeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assertEqual(seed(self.root).returncode, 0)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.workspace)], check=True, capture_output=True, timeout=5)
        self.calculator = self.workspace / "calculator.py"
        self.calculator.write_text("def add(a, b):\n    return a - b\n")
        self.checks = self.workspace / "checks.py"
        self.checks.write_text("from calculator import add\nassert add(17, 29) == 46\n")
        self.checksum = hashlib.sha256(self.checks.read_bytes()).hexdigest()
        self.requests = []
        self.auth_headers = []
        self.mode = "tools"
        self.stage = 0
        self.reasoning = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append(body)
                test.auth_headers.append(self.headers.get("Authorization"))
                if len(test.requests) > 8:
                    self.send_error(429)
                    return
                # Title generation has no tools; it never advances the fixture.
                if not body.get("tools"):
                    delta, finish = {"role": "assistant", "content": "Offline fixture"}, "stop"
                elif test.stage < (1 if test.mode == "ask" else 3):
                    tools = [("read", {"filePath": str(test.calculator)}),
                             ("write", {"filePath": str(test.calculator), "content": "def add(a, b):\n    return a + b\n"}),
                             ("bash", {"command": "python3 checks.py", "description": "Run immutable fixture checks"})]
                    name, args = tools[test.stage]
                    test.stage += 1
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "fixture-" + str(test.stage), "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}}]}
                    finish = "tool_calls"
                else:
                    delta, finish = {"role": "assistant", "content": "Offline fixture finished"}, "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if test.reasoning:
                    reasoning = "preserve reasoning " + str(test.stage)
                    item = {"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": reasoning}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                    if delta.get("tool_calls"):
                        delta["content"] = "fixture plan " + str(test.stage)
                for item in ({"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                             {"id": "fixture", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}):
                    self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = {"$schema": "https://opencode.ai/config.json", "model": "fixture/calculator", "small_model": "fixture/calculator",
                       "enabled_providers": ["fixture"], "autoupdate": False, "permission": {"*": "ask"},
                       "provider": {"fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Offline fixture",
                       "options": {"baseURL": f"http://127.0.0.1:{self.server.server_port}/v1", "apiKey": "fixture-only"},
                       "models": {"calculator": {"name": "calculator", "limit": {"context": 32000, "output": 1000}}}}}}
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / ".config"),
                    "XDG_DATA_HOME": str(self.root / "data"), "XDG_CACHE_HOME": str(self.root / "cache"),
                    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1", "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                    "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost",
                    "PYTHONDONTWRITEBYTECODE": "1", "PWD": str(self.workspace)}

    def invoke(self, *args):
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)
        process = subprocess.Popen([BINARY, *args], cwd=self.workspace, env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            output, error = process.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("native CLI timed out; process group terminated")
        return process.returncode, output, error

    def test_native_version_help_config_and_provider_model(self):
        self.assertEqual(self.invoke("--version")[:2], (0, "1.19.3\n"))
        help_result = self.invoke("run", "--help")
        self.assertEqual(help_result[0], 0)
        self.assertIn("--format", help_result[1] + help_result[2])
        code, output, error = self.invoke("debug", "config")
        self.assertEqual(code, 0, error)
        config = json.loads(output)
        self.assertEqual(config["model"], "fixture/calculator")
        self.assertEqual(config["permission"]["*"], "ask")
        self.assertFalse(config["autoupdate"])
        self.assertIn("fixture/calculator", self.invoke("models", "fixture")[1])
        self.assertEqual(self.requests, [])

    def test_native_run_file_tools_tests_and_export_with_only_scoped_fixture_permissions(self):
        self.config["permission"].update({"read": {"calculator.py": "allow"}, "edit": {"calculator.py": "allow"},
                                          "bash": {"python3 checks.py": "allow"}})
        code, output, error = self.invoke("run", "--format", "json", "Repair the isolated calculator and run its checks.")
        self.assertEqual(code, 0, error)
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertFalse(any(event["type"] == "error" for event in events))
        tools = [event["part"] for event in events if event["type"] == "tool_use"]
        self.assertEqual([tool["tool"] for tool in tools], ["read", "write", "bash"])
        self.assertTrue(all(tool["state"]["status"] == "completed" for tool in tools), tools)
        self.assertEqual(self.calculator.read_text(), "def add(a, b):\n    return a + b\n")
        self.assertEqual(hashlib.sha256(self.checks.read_bytes()).hexdigest(), self.checksum)
        check = subprocess.run([sys.executable, "checks.py"], cwd=self.workspace, env=self.env, capture_output=True, timeout=5)
        self.assertEqual(check.returncode, 0)
        self.assertTrue(all(request["model"] == "calculator" for request in self.requests))
        session = events[0]["sessionID"]
        code, exported, error = self.invoke("export", session)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(exported)["info"]["id"], session)
        code, followup, error = self.invoke("run", "--format", "json", "--session", session, "Confirm the existing isolated result.")
        self.assertEqual(code, 0, error)
        followup_events = [json.loads(line) for line in followup.splitlines() if line.startswith("{")]
        self.assertTrue(followup_events)
        self.assertTrue(all(event["sessionID"] == session for event in followup_events))
        self.assertFalse(any(event["type"] == "error" for event in followup_events))
        self.assertEqual(hashlib.sha256(self.checks.read_bytes()).hexdigest(), self.checksum)
        self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_native_headless_ask_rejects_without_modification_or_hanging(self):
        self.mode = "ask"
        before = self.calculator.read_bytes()
        _, output, error = self.invoke("run", "--format", "json", "Read the isolated calculator.")
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        tools = [event["part"] for event in events if event["type"] == "tool_use"]
        self.assertEqual(len(tools), 1, error)
        self.assertEqual(tools[0]["state"]["status"], "error")
        self.assertIn("auto-rejecting", output + error)
        self.assertEqual(self.calculator.read_bytes(), before)

    def test_native_missing_auth_fails_without_fallback_or_fixture_requests(self):
        self.config = managed.initial_config(KIMI)
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)
        self.env["PYTHONPATH"] = str(LAUNCHER.parent)
        command = [sys.executable, "-c", "import sys,workspace_opencode as m;m.BINARY=sys.argv.pop(1);sys.exit(m.main())", BINARY]
        result = subprocess.run([*command, "run", "--format", "json", "Must fail without authentication."],
                                cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("HF authentication is missing", result.stderr)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_native_invalid_config_and_unknown_model_fail_without_provider_requests(self):
        for change in ({"model": "fixture/missing"}, {"model": 42}):
            with self.subTest(change=change):
                original = self.config.copy()
                self.config.update(change)
                code, output, error = self.invoke("run", "--format", "json", "Must fail locally.")
                self.assertNotEqual(code, 0, output + error)
                self.assertEqual(self.requests, [])
                self.config = original


    def test_native_hf_routes_preserve_reasoning_tools_content_effort_and_followup(self):
        self.reasoning = True
        for ref in (KIMI, MUSE, DEEPSEEK):
            with self.subTest(ref=ref):
                self.requests.clear()
                self.stage = 0
                self.calculator.write_text("def add(a, b):\n    return a - b\n")
                self.config = managed.initial_config(ref)
                # Same production provider/model/adapter; only transport and auth
                # are replaced by a bounded, loopback-only scripted fixture.
                self.config["provider"]["huggingface"]["options"].update(
                    baseURL=f"http://127.0.0.1:{self.server.server_port}/v1", apiKey="fixture-only")
                self.config["permission"].update({"read": {"calculator.py": "allow"}, "edit": {"calculator.py": "allow"},
                                                  "bash": {"python3 checks.py": "allow"}})
                code, output, error = self.invoke("run", "--format", "json", "Repair and check the isolated calculator.")
                self.assertEqual(code, 0, error)
                events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
                self.assertFalse(any(event["type"] == "error" for event in events), output + error)
                tools = [event["part"] for event in events if event["type"] == "tool_use"]
                self.assertEqual([tool["tool"] for tool in tools], ["read", "write", "bash"])
                self.assertTrue(all(tool["state"]["status"] == "completed" for tool in tools), tools)
                self.assertEqual(self.calculator.read_text(), "def add(a, b):\n    return a + b\n")
                session = events[0]["sessionID"]
                code, followup, error = self.invoke("run", "--format", "json", "--session", session, "Confirm the result.")
                self.assertEqual(code, 0, error)
                self.assertTrue(self.requests)
                for request in self.requests:
                    self.assertEqual(request["model"], ref.split("/", 1)[1])
                    self.assertEqual(request["reasoning_effort"], "high")
                history = self.requests[-1]["messages"]
                calls = [message for message in history if message.get("tool_calls")]
                self.assertEqual(len(calls), 3, history)
                for index, message in enumerate(calls, 1):
                    self.assertEqual(message["reasoning_content"], "preserve reasoning " + str(index))
                    self.assertEqual(message["content"], "fixture plan " + str(index))
                    self.assertEqual(message["tool_calls"][0]["id"], "fixture-" + str(index))
                self.assertEqual(len([m for m in history if m["role"] == "tool"]), 3)
                self.assertTrue(any(m.get("reasoning_content") == "preserve reasoning 3" and not m.get("tool_calls") for m in history))
                self.assertEqual(hashlib.sha256(self.checks.read_bytes()).hexdigest(), self.checksum)
                self.assertFalse((self.root / "data/opencode/auth.json").exists())
                for variant in managed.ROUTES[ref.split("/", 1)[1]]["efforts"]:
                    self.requests.clear()
                    code, output, error = self.invoke("run", "--format", "json", "--variant", variant, "Offline variant fixture.")
                    self.assertEqual(code, 0, error)
                    main_requests = [request for request in self.requests if request.get("tools")]
                    self.assertTrue(main_requests)
                    self.assertTrue(all(request.get("reasoning_effort") == variant for request in main_requests), main_requests)

    def test_native_hf_auth_sources_match_guard_without_modifying_fixture_auth(self):
        self.config = managed.initial_config(KIMI)
        self.config["provider"]["huggingface"]["options"]["baseURL"] = f"http://127.0.0.1:{self.server.server_port}/v1"
        auth = self.root / "data/opencode/auth.json"
        self.stage = 3
        for source in ("env", "file", "inline"):
            with self.subTest(source=source):
                self.requests.clear()
                self.auth_headers.clear()
                self.env.pop("HF_TOKEN", None)
                self.env.pop("OPENCODE_AUTH_CONTENT", None)
                auth.unlink(missing_ok=True)
                content = '{"huggingface":{"type":"api","key":"fixture-only"}}'
                if source == "env":
                    self.env["HF_TOKEN"] = "fixture-only"
                elif source == "inline":
                    self.env["OPENCODE_AUTH_CONTENT"] = content
                else:
                    auth.parent.mkdir(parents=True, exist_ok=True)
                    auth.write_text(content)
                    auth.chmod(0o600)
                self.assertTrue(managed.has_hf_auth(self.config, self.env))
                code, output, error = self.invoke("run", "--format", "json", "Offline authentication fixture.")
                self.assertEqual(code, 0, error)
                self.assertTrue(self.requests)
                self.assertTrue(all(header == "Bearer fixture-only" for header in self.auth_headers))
                if source == "file":
                    self.assertEqual(auth.read_text(), content)
                    self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
                else:
                    self.assertFalse(auth.exists())

    def test_native_managed_launcher_readback_allows_only_reviewed_models_and_preserves_files(self):
        original = self.root / ".config/opencode/opencode.json"
        before = original.read_bytes()
        self.config = managed.initial_config(MUSE)
        self.config["provider"]["huggingface"]["options"]["apiKey"] = "fixture-only"
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)
        self.env["PYTHONPATH"] = str(LAUNCHER.parent)
        command = [sys.executable, "-c", "import sys,workspace_opencode as m;m.BINARY=sys.argv.pop(1);sys.exit(m.main())", BINARY]
        result = subprocess.run([*command, "models", "huggingface"], cwd=self.workspace, env=self.env,
                                capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), set(managed.references()))
        self.config["provider"]["huggingface"]["options"]["baseURL"] = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.config)
        result = subprocess.run([*command, "run", "Must reject local endpoint override."], cwd=self.workspace, env=self.env,
                                capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 2)
        self.assertIn("official router", result.stderr)
        self.assertNotIn("fixture-only", result.stderr + result.stdout)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / "data/opencode/auth.json").exists())


class OpenCodeRoutingTest(unittest.TestCase):
    def test_reviewed_routes_and_schema_remain_in_sync(self):
        self.assertEqual(set(managed.references()), {KIMI, MUSE, DEEPSEEK})
        schema = (ROOT / "variables.tf").read_text().replace("[.]", ".")
        receipt = json.loads((ROOT / "tests/fixtures/opencode-hf-catalog.json").read_text())
        self.assertFalse(receipt["authenticated_inference"])
        self.assertEqual({"huggingface/" + item["model"] + ":" + item["provider"] for item in receipt["routes"]}, set(managed.references()))
        for item in receipt["routes"]:
            self.assertEqual(item["status"], "live")
            self.assertTrue(item["supports_tools"])
            self.assertEqual(item["context_length"], 1048576)
        muse = next(item for item in receipt["routes"] if item["model"].startswith("meta-models/"))
        self.assertNotEqual(muse["providerId"], muse["model"])
        for ref in managed.references():
            self.assertIn(ref.split("/", 1)[1].split(":", 1)[0], schema)
            config = managed.initial_config(ref)
            self.assertEqual(config["small_model"], ref)
            self.assertEqual(managed.validate_config(config, []), ref.split("/", 1)[1])
            for variant in managed.ROUTES[ref.split("/", 1)[1]]["efforts"]:
                managed.validate_config(config, ["run", "--variant", variant])

    def test_unsupported_routes_efforts_and_provider_overrides_fail(self):
        mutations = [
            lambda c: c.update(model="openai/gpt-4.1"),
            lambda c: c.update(small_model="openai/gpt-4.1"),
            lambda c: c.update(model="huggingface/deepseek-ai/DeepSeek-V4.1-Flash-1017:baseten"),
            lambda c: c.update(enabled_providers=["huggingface", "openai"]),
            lambda c: c.update(agent={"build": {"model": "openai/gpt-4.1"}}),
            lambda c: c.update(agent={"build": {"variant": "medium"}}),
            lambda c: c.update(agent={"build": {"options": {"reasoningEffort": "off"}}}),
            lambda c: c.update(command={"repair": {"model": "openai/gpt-4.1"}}),
            lambda c: c["provider"]["huggingface"]["options"].update(baseURL="https://other.invalid/v1"),
            lambda c: c["provider"]["huggingface"]["models"]["moonshotai/Kimi-K3:baseten"].update(interleaved=False),
            lambda c: c["provider"]["huggingface"]["models"]["moonshotai/Kimi-K3:baseten"].update(headers={"Host": "other.invalid"}),
            lambda c: c["provider"]["huggingface"]["models"]["moonshotai/Kimi-K3:baseten"]["options"].update(reasoningEffort="medium"),
            lambda c: c["provider"]["huggingface"]["models"]["deepseek-ai/DeepSeek-V4.1-Flash:baseten"]["options"].update(reasoningEffort=50),
        ]
        for mutation in mutations:
            config = managed.initial_config(KIMI)
            mutation(config)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                managed.validate_config(config, [])
        for args in (["run", "--model", "openai/model"], ["run", "--model=openai/model"],
                     ["run", "--variant", "medium"], ["run", "--variant", "off"], ["run", "--variant", "none"], ["run", "--model", DEEPSEEK, "--variant", "low"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                managed.validate_config(managed.initial_config(KIMI), args)

    def test_launch_readback_errors_no_config_leak_or_exec(self):
        secret = "fixture-secret-never-echo"
        with patch.object(managed.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, secret, secret)), patch.object(managed.os, "execve") as execute:
            with self.assertRaisesRegex(ValueError, "cannot resolve") as failure:
                managed.launch(["run", "task"])
            self.assertNotIn(secret, str(failure.exception))
            execute.assert_not_called()
        for args in (["run", "--attach=http://localhost:1", "task"], ["serve"], ["--log-level", "ERROR", "serve"], ["web"], ["acp"], ["upgrade"], ["run", "--auto", "task"]):
            with self.subTest(args=args), self.assertRaises(ValueError), patch.object(managed.subprocess, "run") as native:
                managed.launch(args)
                native.assert_not_called()

    def test_launch_enforces_provider_and_small_model_in_memory_without_writes(self):
        config = managed.initial_config(KIMI)
        config["permission"] = {"*": "ask", "read": "allow"}
        config["provider"]["huggingface"]["options"]["apiKey"] = "fixture-only"
        config.pop("small_model")
        with patch.object(managed.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(config), "")), patch.object(managed.os, "execve") as execute:
            managed.launch(["run", "two words"])
        binary, args, env = execute.call_args.args
        self.assertEqual(args, [managed.BINARY, "run", "two words"])
        final = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(final["enabled_providers"], ["huggingface"])
        self.assertEqual(final["small_model"], KIMI)
        self.assertEqual(final["provider"]["huggingface"]["options"]["baseURL"], managed.ENDPOINT)
        self.assertEqual(final["permission"], config["permission"])
        self.assertEqual(env["OPENCODE_DISABLE_AUTOUPDATE"], "1")

    def test_auth_presence_uses_only_native_hf_sources_and_preserves_fixture_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = {"HOME": temporary, "XDG_DATA_HOME": str(Path(temporary) / "data")}
            config = managed.initial_config(KIMI)
            self.assertFalse(managed.has_hf_auth(config, env))
            self.assertTrue(managed.has_hf_auth(config, dict(env, HF_TOKEN="fixture-only")))
            config["provider"]["huggingface"]["options"]["apiKey"] = "fixture-only"
            self.assertTrue(managed.has_hf_auth(config, env))
            del config["provider"]["huggingface"]["options"]["apiKey"]
            auth = Path(env["XDG_DATA_HOME"]) / "opencode/auth.json"
            auth.parent.mkdir(parents=True)
            auth.write_text('{"huggingface":{"type":"api","key":"fixture-only"}}')
            before = auth.read_bytes()
            self.assertTrue(managed.has_hf_auth(config, env))
            self.assertTrue(managed.has_hf_auth(config, dict(env, OPENCODE_AUTH_CONTENT=before.decode())))
            self.assertFalse(managed.has_hf_auth(config, dict(env, OPENCODE_AUTH_CONTENT="{}")))
            self.assertEqual(auth.read_bytes(), before)
