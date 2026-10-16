import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "test_containers.py"
spec = importlib.util.spec_from_file_location("container_checks", SCRIPT)
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def result(output="", code=0):
    return subprocess.CompletedProcess([], code, output, "")


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.checker = checks.ContainerChecks({}, "fixture-owner")
        self.checker.remember("container", "owned-container")

    def test_confirmed_owned_resource_is_removed_then_independently_absent(self):
        def docker(*args, **kwargs):
            if args[:2] == ("container", "inspect"):
                return result(json.dumps([{"Config": {"Labels": {checks.LABEL: "fixture-owner"}}}]))
            return result()
        with patch.object(self.checker, "docker", side_effect=docker) as commands:
            self.assertTrue(self.checker.cleanup())
        self.assertIn((("container", "rm", "-f", "owned-container"),), [(call.args,) for call in commands.call_args_list])
        self.assertTrue(any(call.args[:2] == ("container", "ls") for call in commands.call_args_list))

    def test_foreign_or_malformed_resource_is_never_removed(self):
        for inspection in ([{"Config": {"Labels": {checks.LABEL: "someone-else"}}}], [{"Config": None}], [{"Config": {"Labels": None}}], [None], []):
            with self.subTest(inspection=inspection):
                def docker(*args, **kwargs):
                    return result(json.dumps(inspection)) if args[:2] == ("container", "inspect") else result()
                with patch.object(self.checker, "docker", side_effect=docker) as commands:
                    self.assertFalse(self.checker.cleanup())
                self.assertFalse(any("rm" in call.args for call in commands.call_args_list))

    def test_failed_inspect_is_not_a_deletion_receipt(self):
        with patch.object(self.checker, "docker", side_effect=checks.CheckFailed("daemon_disconnected")):
            self.assertFalse(self.checker.cleanup())
        def docker(*args, **kwargs):
            if args[:2] == ("container", "inspect"):
                return result(code=1)
            return result("owned-container\n") if args[:2] == ("container", "ls") else result()
        with patch.object(self.checker, "docker", side_effect=docker):
            self.assertFalse(self.checker.cleanup())


class ProcessAndInputTest(unittest.TestCase):
    def test_failed_process_preserves_bounded_output_without_arguments_or_input(self):
        code = "import sys;print('x'*20000);sys.stderr.write('docker-build-failure');sys.exit(42)"
        with self.assertRaises(checks.CheckFailed) as raised:
            checks.execute(["python3", "-c", code, "private-argument"], os.environ, input="private-input")
        error = raised.exception
        self.assertEqual(str(error), "command_failed: python3")
        self.assertEqual(error.details["exit_code"], 42)
        self.assertEqual(error.details["stderr"], "docker-build-failure")
        self.assertEqual(len(error.details["stdout"]), 8192)
        self.assertTrue(error.details["truncated"])
        self.assertFalse(error.details["timed_out"])
        self.assertNotIn("private-", json.dumps(error.details))

    def test_timeout_preserves_partial_output_and_stable_exception(self):
        code = "import sys,time;print('partial-build',flush=True);time.sleep(10)"
        with self.assertRaisesRegex(checks.CheckFailed, "^command_timed_out$") as raised:
            checks.execute(["python3", "-c", code], os.environ, timeout=.2)
        self.assertEqual(raised.exception.details["stdout"], "partial-build\n")
        self.assertTrue(raised.exception.details["timed_out"])
        self.assertLess(raised.exception.details["exit_code"], 0)

    def test_timeout_kills_children_before_they_can_write(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-finished"
            child = "import time; from pathlib import Path; time.sleep(.5); Path(" + repr(str(marker)) + ").touch()"
            parent = "import subprocess,time; subprocess.Popen(['python3','-c'," + repr(child) + "]); time.sleep(5)"
            with self.assertRaises(checks.CheckFailed):
                checks.execute(["python3", "-c", parent], os.environ, timeout=.2)
            time.sleep(.5)
            self.assertFalse(marker.exists())

    def test_registry_and_terraform_inputs_are_isolated_from_user_credentials(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TF_VAR_gateway_token": "secret", "AWS_SECRET_ACCESS_KEY": "secret", "GOOGLE_APPLICATION_CREDENTIALS": "private-path",
            "OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret", "DOCKER_CONFIG": "private-docker-config"
        }):
            env = checks.isolated_env(Path(temp))
            for key in ("TF_VAR_gateway_token", "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                self.assertNotIn(key, env)
            self.assertEqual(env["DOCKER_CONFIG"], str(Path(temp) / "docker-client"))
            self.assertEqual(Path(env["TF_CLI_CONFIG_FILE"]).read_text(), "")

    def test_pin_rejects_similar_but_different_version(self):
        self.assertTrue(checks.pinned_version("codex-cli 0.145.0", "0.145.0"))
        for output in ("codex-cli 0.145.01", "codex-cli 10.145.0", "codex-cli 0.146.0"):
            self.assertFalse(checks.pinned_version(output, "0.145.0"))

    @unittest.skipUnless(shutil.which("terraform"), "Terraform binary unavailable")
    def test_real_terraform_renders_legacy_and_combined_workspace_without_providers(self):
        for claude in (None, "2.1.211"):
            with self.subTest(claude=claude), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                context = checks.render_workspace(root, checks.isolated_env(root), "0.145.0", claude)
                dockerfile = (context / "Dockerfile").read_text()
                self.assertIn("ARG CODEX_RELEASE=0.145.0", dockerfile)
                self.assertEqual("ARG CLAUDE_RELEASE=" in dockerfile, claude is not None)
                self.assertFalse(list(root.glob("**/terraform.tfstate")))
                self.assertTrue((context / "workspace-cli-task.py").is_file())


class MuseTemplateCompatibilityTest(unittest.TestCase):
    def test_isolated_environment_excludes_both_meta_key_names(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "META_API_KEY": "fixture-muse-key", "MODEL_API_KEY": "fixture-model-key"}):
            environment = checks.isolated_env(Path(directory))
        self.assertNotIn("META_API_KEY", environment)
        self.assertNotIn("MODEL_API_KEY", environment)

    @unittest.skipUnless(shutil.which("terraform"), "Terraform binary unavailable")
    def test_real_templates_keep_muse_optional_and_accept_a_pinned_selection(self):
        for muse in (None, "1.4.10-R6427.1"):
            with self.subTest(muse=muse), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context = checks.render_workspace(root, checks.isolated_env(root), "0.145.0", None, muse_release=muse)
                dockerfile = (context / "Dockerfile").read_text()
                entrypoint = (context / "workspace-entrypoint.sh").read_text()
                self.assertIn("ARG CODEX_RELEASE=0.145.0", dockerfile)
                self.assertEqual("ARG MUSE_RELEASE=" in dockerfile, muse is not None)
                self.assertEqual("MUSE_WORKSPACE_GROUP=" in entrypoint, muse is not None)
                self.assertNotIn("%{", entrypoint)
                if muse:
                    self.assertIn("sha256sum -c -", dockerfile)
                    self.assertIn('if [ -z "${META_API_KEY:-}" ]', dockerfile)
                self.assertFalse(list(root.glob("**/terraform.tfstate")))


class ProfileRestartTest(unittest.TestCase):
    def test_current_ssh_binding_rejects_public_or_invalid_ports(self):
        checker = checks.ContainerChecks({}, "fixture-owner")
        for binding in ("0.0.0.0:49000", "[::]:49000", "127.0.0.1:0", "127.0.0.1:65536", "", "127.0.0.1:49000\n127.0.0.1:49001"):
            with self.subTest(binding=binding), patch.object(checker, "docker", return_value=result(binding)):
                with self.assertRaisesRegex(checks.CheckFailed, "^workspace_port_not_loopback$"):
                    checker.ssh_port("owned-fixture")

    def test_ssh_readiness_timeout_preserves_last_connection_error(self):
        checker = checks.ContainerChecks({}, "fixture-owner")
        attempt = subprocess.CompletedProcess([], 255, "", "fixture-connection-refused")
        with patch.object(checks, "execute", return_value=attempt), patch.object(checks.time, "monotonic", side_effect=(0, 1, 121)), \
                patch.object(checks.time, "sleep"), patch.object(checker, "docker", return_value=result('[{"State":{"Running":true}}]')):
            with self.assertRaisesRegex(checks.CheckFailed, "^workspace_ssh_timeout$") as raised:
                checker.wait_ssh("owned-fixture", "49000", Path("fixture-key"), Path("fixture-hosts"))
        self.assertEqual(raised.exception.details["program"], "ssh")
        self.assertEqual(raised.exception.details["stderr"], "fixture-connection-refused")
        self.assertEqual(raised.exception.details["exit_code"], 255)
        self.assertNotIn("fixture-key", json.dumps(raised.exception.details))

    def test_ssh_follows_changed_loopback_port_after_restart_and_recreation(self):
        for claude in (None, "2.1.211"):
            with self.subTest(claude=claude), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                context = root / "context"; context.mkdir()
                checker = checks.ContainerChecks({}, "fixture-owner")
                state = {"port": "49000", "starts": 0, "disabled": set()}
                connections = []
                def docker(*args, **kwargs):
                    if args[0] == "port": return result("127.0.0.1:" + state["port"] + "\n")
                    if args[0] == "restart": state["port"] = "49001"
                    if args[0] == "run":
                        if any(str(value).endswith("-missing-password") for value in args):
                            return result("WORKSPACE_PASSWORD is required", code=1)
                        state["starts"] += 1
                        if state["starts"] == 2: state["port"] = "49002"
                    if args[0] == "exec" and args[2] == "ssh-keygen": return result("fixture-persisted-host-key")
                    if args[0] == "exec" and args[2] == "mv":
                        tool = Path(args[3]).name.removesuffix(".test-disabled")
                        if args[4].endswith(".test-disabled"): state["disabled"].add(tool)
                        else: state["disabled"].remove(tool)
                    return result()
                def wait(name, port, key, hosts):
                    self.assertEqual(port, state["port"], "SSH must use Docker's current published port")
                    connections.append(port)
                    return ["fixture-ssh", port]
                def execute(args, env, **kwargs):
                    if args[0] == "docker": return docker(*args[1:], **kwargs)
                    if args[0] == "ssh-keygen":
                        Path(args[-1]).with_suffix(".pub").write_text("fixture-public-key")
                        return result()
                    if args[0] == "openssl": return result("$6$" + "a" * 16 + "$" + "b" * 86)
                    self.assertEqual(args[0], "fixture-ssh")
                    self.assertEqual(args[1], state["port"], "Subsequent commands must follow the refreshed SSH endpoint")
                    task = args[-1]
                    if task == "command -v claude": return result(code=1)
                    if task.endswith(" --version"):
                        tool = task.split()[0]
                        return result(code=127) if tool in state["disabled"] else result("0.145.0" if tool == "codex" else claude)
                    if "python3" in task: return result(json.dumps({"uid": 1000, "owner": 1000, "home": "/home/testuser"}))
                    self.assertIn("container-test-marker", task)
                    return result()
                with patch.object(checker, "docker", side_effect=docker), patch.object(checker, "wait_ssh", side_effect=wait), \
                        patch.object(checks, "execute", side_effect=execute):
                    report = checker.profile(root, "0.145.0", claude, "linux/amd64", context=context)
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["persistence"], "pass")
                self.assertEqual(connections, ["49000", "49001", "49002"])
