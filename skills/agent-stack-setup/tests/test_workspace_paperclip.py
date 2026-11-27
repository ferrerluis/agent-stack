from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
import unittest
from unittest.mock import patch
import uuid
import urllib.error


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "modules/common/templates/runtime/workspace-paperclip.py"
SPEC = importlib.util.spec_from_file_location("workspace_paperclip", SCRIPT)
paperclip = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paperclip)


class FixtureRuntime(paperclip.Runtime):
    """Only the external process/API boundary is faked; use real setup files."""

    def __init__(self, home, db=None):
        super().__init__(home)
        self.db = db if db is not None else {"companies": [], "agents": [], "runs": []}
        self.requests = []
        self.started = []
        self.stopped = 0
        self.fail_after_company_create = False
        self.fail_after_agent_create = False

    def start(self):
        self.started.append(paperclip.read_json(self.config_path)["server"])

    def stop(self):
        self.stopped += 1

    def wait_bootstrap(self):
        pass

    def api(self, method, path, value=None):
        self.requests.append((method, path, copy.deepcopy(value)))
        if method == "GET":
            if path.endswith("/agents"):
                return copy.deepcopy(self.db["agents"])
            if path.endswith("/heartbeat-runs"):
                return copy.deepcopy(self.db["runs"])
            if path == "/companies":
                return copy.deepcopy(self.db["companies"])
        elif method == "POST" and path == "/companies":
            company = {**value, "id": str(uuid.uuid4())}
            self.db["companies"].append(company)
            if self.fail_after_company_create:
                self.fail_after_company_create = False
                raise paperclip.SetupError("lost company response")
            return copy.deepcopy(company)
        elif method == "POST" and path.endswith("/agents"):
            found = next((agent for agent in self.db["agents"] if agent["id"] == value["setupId"]), None)
            if found is None:
                found = {**value, "id": value["setupId"], "companyId": path.split("/")[2], "status": "paused", "lifecycleState": "paused", "lifecycleHolds": ["setup"]}
                self.db["agents"].append(found)
            if self.fail_after_agent_create:
                self.fail_after_agent_create = False
                raise paperclip.SetupError("lost agent response")
            return copy.deepcopy(found)
        raise AssertionError(f"Unexpected API call: {method} {path}")


class PaperclipSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.runtime = FixtureRuntime(self.home)

    def test_fresh_setup_creates_one_credential_free_paused_draft(self):
        state = self.runtime.initialize()
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(len(self.runtime.db["companies"]), 1)
        self.assertEqual(self.runtime.db["companies"][0]["name"], "ACME Co")
        self.assertEqual(len(self.runtime.db["agents"]), 1)
        agent = self.runtime.db["agents"][0]
        self.assertTrue(self.runtime.is_setup_draft(agent, state["companyId"], state["setupId"]))
        self.assertFalse(agent["adapterConfig"]["dangerouslyBypassApprovalsAndSandbox"])
        self.assertEqual(set(agent["adapterConfig"]["env"]), {"CODEX_HOME"})
        self.assertEqual(self.runtime.db["runs"], [])
        self.assertEqual([path for method, path, _ in self.runtime.requests if method == "POST"], ["/companies", f"/companies/{state['companyId']}/agents"])

    def test_only_temporary_listener_is_trusted_and_is_never_published(self):
        self.runtime.initialize()
        self.assertEqual(len(self.runtime.started), 1)
        self.assertEqual(self.runtime.started[0]["host"], "127.0.0.1")
        self.assertEqual(self.runtime.started[0]["port"], 3101)
        self.assertFalse(self.runtime.started[0]["serveUi"])
        config = paperclip.read_json(self.runtime.config_path)
        self.assertEqual(config["server"]["deploymentMode"], "authenticated")
        self.assertEqual(config["server"]["port"], 3100)
        self.assertEqual(config["server"]["exposure"], "private")
        self.assertIn("paperclip", config["server"]["allowedHostnames"])
        self.assertNotIn("paperclip", self.runtime.started[0]["allowedHostnames"])
        self.assertNotIn("llm", config)

    def test_repeated_start_never_reseeds_downgrades_or_unpauses(self):
        first = self.runtime.initialize()
        before = self.runtime.config_path.read_bytes()
        second = FixtureRuntime(self.home, self.runtime.db)
        self.assertEqual(second.initialize(), first)
        self.assertEqual(second.requests, [])
        self.assertEqual(second.started, [])
        self.assertEqual(second.config_path.read_bytes(), before)
        self.assertEqual(second.db["agents"][0]["status"], "paused")

    def test_retry_after_lost_company_response_does_not_duplicate(self):
        self.runtime.fail_after_company_create = True
        with self.assertRaisesRegex(paperclip.SetupError, "lost company response"):
            self.runtime.initialize()
        self.assertEqual(paperclip.read_json(self.runtime.state_path)["phase"], "initializing")
        self.runtime.initialize()
        self.assertEqual(len(self.runtime.db["companies"]), 1)
        self.assertEqual(len(self.runtime.db["agents"]), 1)

    def test_retry_after_lost_agent_response_keeps_deterministic_setup_id(self):
        self.runtime.fail_after_agent_create = True
        with self.assertRaisesRegex(paperclip.SetupError, "lost agent response"):
            self.runtime.initialize()
        first_id = self.runtime.db["agents"][0]["id"]
        self.runtime.initialize()
        self.assertEqual(len(self.runtime.db["agents"]), 1)
        self.assertEqual(self.runtime.db["agents"][0]["id"], first_id)

    def test_unexpected_or_active_agent_prevents_authenticated_start(self):
        state = self.runtime.prepare()
        self.runtime.db["companies"] = [{"id": str(uuid.uuid4()), "name": "ACME Co"}]
        self.runtime.db["agents"] = [{"id": state["setupId"], "companyId": self.runtime.db["companies"][0]["id"], "status": "idle"}]
        with self.assertRaisesRegex(paperclip.SetupError, "paused setup hold"):
            self.runtime.initialize()
        self.assertEqual(paperclip.read_json(self.runtime.state_path)["phase"], "initializing")
        self.assertEqual(self.runtime.db["agents"][0]["status"], "idle")

    def test_unexpected_runs_prevent_publishing(self):
        self.runtime.db["runs"] = [{"id": "unexpected-run"}]
        with self.assertRaisesRegex(paperclip.SetupError, "Unexpected agent runs"):
            self.runtime.initialize()
        self.assertEqual(paperclip.read_json(self.runtime.state_path)["phase"], "initializing")

    def test_preexisting_unmanaged_instance_is_not_adopted(self):
        self.runtime.instance.mkdir(parents=True)
        with self.assertRaisesRegex(paperclip.SetupError, "manual adoption"):
            self.runtime.initialize()
        self.assertEqual(self.runtime.started, [])

    def test_missing_marker_on_initialized_instance_fails_closed(self):
        self.runtime.initialize()
        self.runtime.state_path.unlink()
        with self.assertRaisesRegex(paperclip.SetupError, "manual adoption"):
            self.runtime.initialize()

    def test_completed_setup_preserves_a_deliberately_customized_company_name(self):
        self.runtime.initialize()
        self.runtime.db["companies"][0]["name"] = "User Customized Company"
        restarted = FixtureRuntime(self.home, self.runtime.db)
        restarted.initialize()
        self.assertEqual(restarted.db["companies"][0]["name"], "User Customized Company")
        self.assertEqual(restarted.requests, [])

    def test_interrupted_setup_uses_saved_identity_after_company_rename(self):
        self.runtime.fail_after_agent_create = True
        with self.assertRaises(paperclip.SetupError):
            self.runtime.initialize()
        self.runtime.db["companies"][0]["name"] = "User Customized Company"
        self.runtime.initialize()
        self.assertEqual(len(self.runtime.db["companies"]), 1)
        self.assertEqual(self.runtime.db["companies"][0]["name"], "User Customized Company")

    def test_repeated_setup_rejects_authentication_downgrade(self):
        self.runtime.initialize()
        config = paperclip.read_json(self.runtime.config_path)
        config["server"]["deploymentMode"] = "local_trusted"
        paperclip.write_json(self.runtime.config_path, config)
        with self.assertRaisesRegex(paperclip.SetupError, "authenticated/private"):
            self.runtime.initialize()

    def test_internal_keys_are_private_stable_and_not_in_container_environment(self):
        self.runtime.initialize()
        paths = [self.runtime.state_path, self.runtime.config_path, self.runtime.instance / ".env", self.runtime.instance / "secrets/master.key"]
        original = {path: path.read_bytes() for path in paths}
        self.runtime.initialize()
        for path in paths:
            self.assertEqual(path.read_bytes(), original[path])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        values = (self.runtime.instance / ".env").read_text().splitlines()
        self.assertEqual(len(values), 3)
        self.assertEqual(len({line.split("=", 1)[1] for line in values}), 3)

    def test_ambient_credentials_and_proxy_are_not_inherited(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-only", "CODEX_HOME": "/fixture/legacy-login", "WORKSPACE_PASSWORD": "fixture-password", "HTTP_PROXY": "http://fixture.invalid"}):
            env = self.runtime.environment()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("WORKSPACE_PASSWORD", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertEqual(env["CODEX_HOME"], str(self.runtime.codex_home))
        self.assertNotEqual(env["CODEX_HOME"], str(self.home / ".codex"))

    def test_symlinked_existing_auth_or_key_paths_are_rejected(self):
        self.runtime.prepare()
        self.runtime.instance.mkdir(parents=True)
        (self.home / "foreign-key").write_text("fixture-only")
        (self.runtime.instance / ".env").symlink_to(self.home / "foreign-key")
        with self.assertRaisesRegex(paperclip.SetupError, "must not be symlinks"):
            self.runtime.initialize()
        self.assertEqual((self.home / "foreign-key").read_text(), "fixture-only")

    def test_corrupt_marker_is_not_repaired_into_fresh_setup(self):
        self.runtime.prepare()
        self.runtime.state_path.write_text('{"schema":1,"phase":"unknown"}')
        with self.assertRaisesRegex(paperclip.SetupError, "refusing to reseed"):
            self.runtime.initialize()
        self.assertEqual(self.runtime.started, [])

    def test_missing_isolation_configuration_prevents_publish(self):
        api = self.runtime.api
        def without_isolation(method, path, value=None):
            result = api(method, path, value)
            if method == "POST" and path.endswith("/agents"):
                result["adapterConfig"].pop("env")
            return result
        with patch.object(self.runtime, "api", side_effect=without_isolation):
            with self.assertRaisesRegex(paperclip.SetupError, "coding-agent isolation"):
                self.runtime.initialize()
        self.assertEqual(paperclip.read_json(self.runtime.state_path)["phase"], "initializing")

    def test_upstream_normalized_redacted_plain_home_binding_is_accepted(self):
        api = self.runtime.api
        def normalized(method, path, value=None):
            result = api(method, path, value)
            if method == "POST" and path.endswith("/agents"):
                result["adapterConfig"]["env"]["CODEX_HOME"] = {"type": "plain", "value": "***REDACTED***"}
            return result
        with patch.object(self.runtime, "api", side_effect=normalized):
            self.assertEqual(self.runtime.initialize()["phase"], "complete")

    def test_health_requires_unauthenticated_company_access_to_be_denied(self):
        self.runtime.initialize()
        error = urllib.error.HTTPError("http://fixture.invalid", 401, "fixture denied", None, None)
        with patch.object(paperclip.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = [io.BytesIO(b'{"status":"ok"}'), error]
            self.runtime.health()

    def test_health_rejects_a_server_that_leaks_unauthenticated_companies(self):
        self.runtime.initialize()
        with patch.object(paperclip.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = [io.BytesIO(b'{"status":"ok"}'), io.BytesIO(b'[]')]
            with self.assertRaisesRegex(paperclip.SetupError, "unauthenticated company"):
                self.runtime.health()


class PaperclipWiringTest(unittest.TestCase):
    def test_companion_only_shares_workspace_home_and_has_no_model_credentials(self):
        compose = (ROOT / "modules/common/templates/runtime/docker-compose.yml.tpl").read_text()
        companion = compose.split("  paperclip:", 1)[1].split("%{ endif }", 1)[0]
        self.assertIn('"127.0.0.1:3100:3100"', companion)
        self.assertIn('user: "1000:1000"', companion)
        self.assertIn("condition: service_healthy", companion)
        self.assertNotIn("env_file", companion)
        self.assertNotIn(".codex:", companion)
        self.assertNotIn("docker.sock", companion)
        self.assertNotIn("API_KEY", companion)

    def test_older_workspace_recipe_rebuilds_without_new_paperclip_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            for name in ["workspace-entrypoint.sh", "workspace-para-memory-drive-skill.md", "workspace-codex-update.sh", "workspace-codex-control.py"]:
                (app / name).write_text("fixture")
            (app / "workspace.Dockerfile").write_text("FROM ubuntu:24.04\nCOPY workspace-entrypoint.sh /entrypoint\n")
            source = (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()
            function = source.split("build_workspace_image() {", 1)[1].split("\n}\n", 1)[0]
            script = '''set -euo pipefail
app="$1"
docker() {
  test "$1" = build
  context="${!#}"
  test -f "$context/workspace-entrypoint.sh"
  test ! -e "$context/workspace-paperclip.py"
  printf 'rebuilt legacy image\\n'
}
build_workspace_image() {''' + function + "\n}\nbuild_workspace_image\n"
            result = subprocess.run(["bash", "-c", script, "fixture", str(app)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("rebuilt legacy image", result.stdout)

    def test_new_recipe_refuses_missing_required_paperclip_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            for name in ["workspace-entrypoint.sh", "workspace-para-memory-drive-skill.md", "workspace-codex-update.sh", "workspace-codex-control.py"]:
                (app / name).write_text("fixture")
            (app / "workspace.Dockerfile").write_text("FROM ubuntu:24.04\nCOPY workspace-paperclip.py /helper\n")
            source = (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()
            function = source.split("build_workspace_image() {", 1)[1].split("\n}\n", 1)[0]
            script = 'set -euo pipefail\napp="$1"\ndocker() { printf "must not build\\n"; }\nbuild_workspace_image() {' + function + "\n}\nbuild_workspace_image\n"
            result = subprocess.run(["bash", "-c", script, "fixture", str(app)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("must not build", result.stdout)


if __name__ == "__main__":
    unittest.main()
