import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import re
import shlex
import unittest

from test_render_tfvars import run_render

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("meta_hermes_config", ROOT / "modules/common/runtime/hermes_config.py")
hermes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermes)


def answers(**overrides):
    return dict(cloud_provider="digitalocean", do_token="do-fixture-value",
                model_providers_enabled=["meta"], meta_api_key="meta-fixture-value",
                default_model="meta/muse-spark-1.3", fallback_models=["meta/muse-wonder-1.0"],
                tailscale_enabled=False, enabled_services=["openclaw", "hermes"], **overrides)


class MetaSetupTest(unittest.TestCase):
    def render(self, **overrides):
        data = answers()
        data.update(overrides)
        return run_render(data)

    def reject(self, **overrides):
        data = answers()
        data.update(overrides)
        return run_render(data, expect_success=False)[0].stderr

    def test_both_models_and_default_harnesses_render(self):
        for model in ("muse-spark-1.3", "muse-wonder-1.0"):
            with self.subTest(model=model):
                _, rendered = self.render(default_model="meta/" + model)
                self.assertIn('default_model = "meta/' + model + '"', rendered)
                self.assertIn('workspace_harnesses = [\n  "codex",\n]', rendered)

    def test_missing_placeholder_and_multiline_meta_keys_are_rejected(self):
        for key in ("", "YOUR_META_API_KEY", "sk-...", "real\nINJECTED=value", "real'quoted"):
            with self.subTest(key=key):
                self.assertIn("meta_api_key", self.reject(meta_api_key=key))

    def test_meta_requires_selected_provider_including_hermes_override(self):
        self.assertIn("model_providers_enabled", self.reject(model_providers_enabled=["google"]))
        self.assertIn("meta_api_key", self.reject(default_model="google/gemini-fixture", fallback_models=[],
            model_providers_enabled=["google", "meta"], gemini_api_key="fixture-gemini", meta_api_key="",
            hermes_model="meta/muse-wonder-1.0"))

    def test_workspace_selection_is_multi_select_with_mandatory_codex(self):
        for harnesses in ([], ["muse_code"], ["codex", "codex"], ["codex", "unknown"]):
            with self.subTest(harnesses=harnesses):
                self.assertIn("workspace_harnesses", self.reject(workspace_harnesses=harnesses))
        _, rendered = self.render(enabled_services=["openclaw", "hermes", "workspace"],
            workspace_password="fixture-password", workspace_harnesses=["codex", "claude_code", "muse_code"],
            workspace_claude_release="2.1.301")
        for harness in ("codex", "claude_code", "muse_code"):
            self.assertIn('"' + harness + '"', rendered)

    def test_optional_tools_require_workspace_and_pinned_releases(self):
        self.assertIn("workspace service", self.reject(workspace_harnesses=["codex", "muse_code"]))
        self.assertIn("workspace_muse_release", self.reject(workspace_muse_release="latest"))
        self.assertIn("workspace_claude_release", self.reject(enabled_services=["workspace"],
            workspace_password="fixture-password", workspace_harnesses=["codex", "claude_code"]))

    def test_muse_requires_configured_key_independent_of_model_provider(self):
        data = dict(enabled_services=["openclaw", "workspace"], workspace_password="fixture-password",
            workspace_harnesses=["codex", "muse_code"], model_providers_enabled=["google"],
            default_model="google/gemini-fixture", fallback_models=[], gemini_api_key="fixture-gemini")
        for key in ("", "   ", "YOUR_META_API_KEY"):
            with self.subTest(key=key):
                self.assertIn("meta_api_key", self.reject(**data, meta_api_key=key))

    def test_muse_key_does_not_require_meta_as_an_agent_provider(self):
        _, rendered = self.render(enabled_services=["openclaw", "workspace"],
            workspace_password="fixture-password", workspace_harnesses=["codex", "muse_code"],
            model_providers_enabled=["google"], default_model="google/gemini-fixture",
            fallback_models=[], gemini_api_key="fixture-gemini", meta_api_key="muse-only-fixture")
        self.assertIn('meta_api_key = "muse-only-fixture"', rendered)
        self.assertIn('default_model = "google/gemini-fixture"', rendered)
        self.assertIn('model_providers_enabled = [\n  "google",\n]', rendered)

    def test_disabled_hermes_does_not_require_an_unused_meta_key(self):
        _, rendered = self.render(enabled_services=["openclaw"], model_providers_enabled=["google"],
            meta_api_key="", default_model="google/gemini-fixture", fallback_models=[],
            gemini_api_key="fixture-gemini", hermes_model="meta/muse-wonder-1.0")
        self.assertIn('meta_api_key = ""', rendered)

    def test_existing_non_meta_defaults_are_unchanged(self):
        _, rendered = self.render(model_providers_enabled=["google"], meta_api_key="", default_model="google/gemini-fixture",
            fallback_models=[], gemini_api_key="fixture-gemini")
        self.assertIn('meta_api_key = ""', rendered)
        self.assertIn('workspace_claude_enabled = false', rendered)


class MetaHermesTest(unittest.TestCase):
    def test_primary_and_ordered_fallbacks_map_to_native_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes.seed_config(directory, {"model_ref": "meta/muse-wonder-1.0",
                "fallback_models": ["meta/muse-spark-1.3", "google/gemini-fixture"]})
            config = json.loads((Path(directory) / "config.yaml").read_text())
            self.assertEqual(config["model"], {"provider": "meta-ai", "default": "muse-wonder-1.0"})
            self.assertEqual(config["fallback_providers"], [
                {"provider": "meta-ai", "model": "muse-spark-1.3"},
                {"provider": "gemini", "model": "gemini-fixture"}])
            self.assertNotIn("api_key", json.dumps(config))

    def test_filtered_global_fallbacks_seed_both_modes_in_order(self):
        seed = {"model_ref": "google/gemini-fixture", "fallback_models": [
            "anthropic/claude-fixture", "openai/gpt-fixture", "google/gemini-backup",
            "groq/llama-fixture", "meta/muse-wonder-1.0"]}
        expected = [
            {"provider": "anthropic", "model": "claude-fixture"},
            {"provider": "custom", "model": "gpt-fixture", "base_url": "https://api.openai.com/v1"},
            {"provider": "gemini", "model": "gemini-backup"},
            {"provider": "groq", "model": "llama-fixture"},
            {"provider": "meta-ai", "model": "muse-wonder-1.0"}]
        for mode in ("auto", "manage"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                if mode == "manage":
                    path.write_text(json.dumps({"channels": {"telegram": True}, "model": {"default": "old"}}))
                self.assertTrue(hermes.seed_config(directory, seed, mode))
                result = json.loads(path.read_text())
                self.assertEqual(result["model"], {"provider": "gemini", "default": "gemini-fixture"})
                self.assertEqual(result["fallback_providers"], expected)
                if mode == "manage":
                    self.assertEqual(result["channels"], {"telegram": True})

    def test_manage_updates_routes_without_discarding_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(json.dumps({"channels": {"telegram": True}, "fallback_model": {"provider": "old"}}))
            hermes.seed_config(directory, {"model_ref": "meta/muse-spark-1.3", "fallback_models": []}, "manage")
            result = json.loads(path.read_text())
            self.assertEqual(result["channels"], {"telegram": True})
            self.assertEqual(result["fallback_providers"], [])
            self.assertNotIn("fallback_model", result)

    def test_auto_and_preserve_do_not_replace_operator_models(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            original = "model:\n  default: operator-model\n"
            path.write_text(original)
            for mode in ("auto", "preserve"):
                self.assertFalse(hermes.seed_config(directory, {"model_ref": "meta/muse-wonder-1.0",
                    "fallback_models": ["openai-codex/operator-fallback"]}, mode))
                self.assertEqual(path.read_text(), original)


class MetaOpenClawBootstrapTest(unittest.TestCase):
    def function(self, name):
        template = (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()
        return re.search(r"^" + name + r"\(\) \{\n.*?^\}", template, re.M | re.S)[0].replace("$${", "${")

    def run_fixture(self, mode, selected=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "set -euo pipefail\napp=" + shlex.quote(directory) + "\n"
            script += "provider_selected() { " + ("return 0" if selected else "return 1") + "; }\n"
            script += "has_env_key() { " + ("return 1" if mode == "missing_key" else "return 0") + "; }\n"
            script += "wait_openclaw_healthy() { return 0; }\nfail() { echo \"$*\" >&2; exit 1; }\n"
            script += "mark_openclaw_restart_needed() { touch \"$app/restart\"; }\n"
            script += "run_openclaw_cli() { printf '%s\\n' \"$*\" >>\"$app/calls\"; "
            if mode == "install_failure":
                script += "echo fixture-private-diagnostic; return 1; }\n"
            elif mode == "already_installed":
                script += "if [ \"$2\" = install ] && [[ \" $* \" != *' --force '* ]]; then echo 'plugin already exists'; return 1; fi; }\n"
            elif mode == "enable_failure":
                script += "[ \"$2\" != enable ]; }\n"
            else:
                script += "return 0; }\n"
            script += 'OPENCLAW_CONFIG="$app/openclaw.json"\n'
            (root / "openclaw.json").write_text(json.dumps({"channels": {"fixture": True}, "models": {"providers": {"meta": {"baseUrl": "https://operator-gateway.example/v1"}}}}))
            provider = json.dumps({"baseUrl": "https://api.meta.ai/v1", "api": "openai-responses", "models": [{"id": "muse-spark-1.3"}, {"id": "muse-wonder-1.0"}]})
            script += "build_meta_provider_catalog() { printf '%s\\n' " + shlex.quote(provider) + "; }\n"
            script += self.function("configure_meta_provider") + "\nconfigure_meta_provider\n"
            script += "jq -e '.channels.fixture == true and .models.providers.meta.baseUrl == \"https://operator-gateway.example/v1\" and .models.providers.meta.apiKey.id == \"MODEL_API_KEY\" and (.models.providers.meta.models | length) == 2' \"$OPENCLAW_CONFIG\" >/dev/null\n" if selected else ""

            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
            calls = (root / "calls").read_text() if (root / "calls").exists() else ""
            return result, calls, (root / "restart").exists()

    def test_selected_meta_installs_pinned_package_and_enables_manifest_id(self):
        result, calls, restart = self.run_fixture("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plugins install @openclaw/meta-provider@2026.10.4", calls)
        self.assertIn("plugins enable meta", calls)
        self.assertTrue(restart)

    def test_provider_is_opt_in_and_existing_install_is_idempotent(self):
        result, calls, restart = self.run_fixture("success", selected=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, "")
        self.assertFalse(restart)
        result, calls, restart = self.run_fixture("already_installed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plugins install @openclaw/meta-provider@2026.10.4 --force", calls)
        self.assertIn("plugins enable meta", calls)
        self.assertTrue(restart)

    def test_missing_key_or_incompatible_plugin_fails_without_dumping_diagnostics(self):
        for mode in ("missing_key", "install_failure", "enable_failure"):
            with self.subTest(mode=mode):
                result, calls, restart = self.run_fixture(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(restart)
                self.assertNotIn("fixture-private-diagnostic", result.stdout + result.stderr)


class MetaCredentialPresenceTest(unittest.TestCase):
    def test_empty_quoted_compose_values_are_not_credentials(self):
        source = (ROOT / "modules/common/templates/runtime/install-agent-stack.sh.tpl").read_text()
        names = ("env_file_value", "env_value", "has_env_key")
        functions = "\n".join(re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S)[0] for name in names)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value, expected in (("", False), ("''", False), ('""', False), ("'fixture-key'", True)):
                with self.subTest(value=value):
                    (root / ".env").write_text("MODEL_API_KEY=" + value + "\n")
                    script = "app=" + shlex.quote(directory) + "\n" + functions + "\nhas_env_key MODEL_API_KEY\n"
                    result = subprocess.run(["bash", "-c", script], capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, expected)
                    self.assertEqual(result.stdout, b"")


class MuseWorkspaceAuthTest(unittest.TestCase):
    def test_user_key_wins_and_ssh_can_load_configured_runtime_key(self):
        template = (ROOT / "modules/common/templates/runtime/workspace.Dockerfile.tpl").read_text()
        line = next(line for line in template.splitlines() if ">/usr/local/bin/muse" in line)
        pieces = re.findall(r"'([^']*)'", line)
        wrapper = "\n".join(pieces[1:]).replace("$${", "${") + "\n"
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keyfile = root / "runtime.env"
            binary = root / "native"
            binary.write_text('#!/bin/sh\nprintf "%s" "$META_API_KEY"\n')
            binary.chmod(0o755)
            script = root / "muse"
            script.write_text(wrapper.replace("/etc/agent-stack-muse.env", str(keyfile)).replace("/usr/local/libexec/muse", str(binary)))
            script.chmod(0o755)
            for supplied, configured, expected in (("", "configured-fixture", "configured-fixture"),
                                                   ("user-fixture", "configured-fixture", "user-fixture"),
                                                   ("user-fixture", "", "user-fixture")):
                with self.subTest(supplied=supplied, configured=configured):
                    keyfile.write_text("export META_API_KEY=" + shlex.quote(configured) + "\n")
                    result = subprocess.run([str(script)], env=dict(os.environ, META_API_KEY=supplied),
                                            text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
