import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import showcase_attempt as attempt
from test_showcase_policy import config, REF
from test_showcase_prepaid import funded


def archive(files, extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as tar:
        for name, value in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            tar.addfile(info, io.BytesIO(value))
        if extra:
            tar.addfile(extra)
    return output.getvalue()


class IsolationTest(unittest.TestCase):
    def test_archive_refuses_state_auth_links_absolute_traversal_and_duplicates(self):
        required = {"main.tf": b"", "variables.tf": b"", "examples/showcase/terraform.tfvars.example": b""}
        for name in ("terraform.tfstate", "terraform.tfstate.backup", "terraform.tfvars.json", ".aws/config", ".codex/auth.json",
                     "id_ed25519", "key.pem", "../outside", "/outside"):
            with self.subTest(name=name), self.assertRaises(attempt.PolicyError):
                attempt.safe_archive(archive({**required, name: b"private"}))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            info = tarfile.TarInfo("unsafe")
            info.type = kind
            info.linkname = "outside"
            with self.assertRaises(attempt.PolicyError):
                attempt.safe_archive(archive(required, info))
        self.assertEqual(set(attempt.safe_archive(archive(required))), set(required))

    def test_all_provider_settings_pin_both_tools_and_force_new_disks_and_private_access(self):
        value = config()
        profile = (attempt.ROOT / "examples/showcase/terraform.tfvars.example").read_text()
        for provider in attempt.PROVIDERS:
            settings = attempt.settings(value, provider, "as7-fixture01", "ssh-ed25519 fixture test", profile)
            self.assertEqual(settings["enabled_services"], ["openclaw", "hermes", "workspace"])
            self.assertFalse(settings["public_domain_enabled"])
            self.assertTrue(settings['openclaw_host_network_enabled'])
            self.assertEqual(settings["allowed_ssh_cidr"], "203.0.113.42/32")
            self.assertEqual(settings["workspace_codex_release"], value["pins"]["codex_release"])
            self.assertEqual(settings["workspace_claude_release"], value["pins"]["claude_release"])
            self.assertFalse(settings["workspace_codex_auto_update_enabled"])
            self.assertTrue(settings["gcp_showcase_cost_controls"])
            self.assertNotIn("workspace_password", settings)
            self.assertNotIn("openai_api_key", settings)
            self.assertTrue(all(item == "" for key, item in settings.items() if "existing_volume" in key))

    def test_profile_change_cannot_open_public_access_or_enable_messaging(self):
        profile = (attempt.ROOT / "examples/showcase/terraform.tfvars.example").read_text()
        for changed in (profile.replace("public_domain_enabled               = false", "public_domain_enabled               = true"),
                        profile + '\nagent_channel = "telegram"\n', profile + '\nextra = null\n',
                        profile.replace("gcp_showcase_cost_controls         = true", "gcp_showcase_cost_controls         = false")):
            with self.assertRaises(attempt.PolicyError):
                attempt.settings(config(), "aws", "as7-fixture01", "ssh-ed25519 fixture", changed)

    def test_funded_guest_settings_enforce_the_selected_trial_egress_cap(self):
        value=funded()
        profile=(attempt.ROOT/'examples/showcase/terraform.tfvars.example').read_text()
        ordinary=attempt.settings(value,'digitalocean','as7-fixture01','ssh-ed25519 fixture',profile)
        self.assertEqual(ordinary['showcase_trial_limits']['egress_cap_bytes'],1073741824)
        for cap in (134217728, 268435456, 536870912):
            value['trial_egress_cap_bytes']=cap
            bounded=attempt.settings(value,'digitalocean','as7-fixture01','ssh-ed25519 fixture',profile)
            self.assertEqual(bounded['showcase_trial_limits']['egress_cap_bytes'],cap)

    def test_environment_ignores_personal_credentials_tf_cli_overrides_proxies_and_agents(self):
        poisoned = {key: "must-not-inherit" for key in ("TF_VAR_gateway_token", "TF_LOG", "TF_CLI_ARGS", "AWS_PROFILE", "AWS_ACCESS_KEY_ID",
                    "GOOGLE_APPLICATION_CREDENTIALS", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OP_SERVICE_ACCOUNT_TOKEN", "SSH_AUTH_SOCK", "HTTPS_PROXY")}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, poisoned):
            env = attempt.isolated_environment(Path(temp))
            self.assertTrue(all(key not in env for key in poisoned))
            self.assertEqual(env["AWS_CONFIG_FILE"], "/dev/null")
            self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], "/dev/null")
            self.assertEqual(env["TF_CLI_CONFIG_FILE"], str(Path(temp) / "terraform.rc"))

    def test_world_readable_directory_or_symlink_ancestor_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            public = root / "public"
            public.mkdir(mode=0o755)
            public.chmod(0o755)  # independent of the wrapper's private umask
            with self.assertRaises(attempt.PolicyError):
                attempt.private_directory(public)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(attempt.PolicyError):
                attempt.private_directory(root / "alias" / "new", create=True)

    def test_deep_private_run_uses_short_distinct_disk_temporary_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            deep = Path(temp) / ("a" * 90)
            deep.mkdir(mode=0o700)
            first = attempt.isolated_environment(deep)
            self.assertLessEqual(len(os.fsencode(first["TMPDIR"])), 75)
            self.assertIn(".tmp", Path(first["TMPDIR"]).parts)
            self.assertEqual(Path(first["TMPDIR"]).stat().st_mode & 0o077, 0)
            other = deep / "other"
            other.mkdir(mode=0o700)
            second = attempt.isolated_environment(other)
            self.assertNotEqual(first["TMPDIR"], second["TMPDIR"])

    def test_deadlines_leave_cleanup_margin_and_reject_expired_or_invalid_values(self):
        deadlines = {"work_deadline": 1100, "deadline": 2000}
        self.assertEqual(attempt.remaining_timeout(deadlines, 1800, now=1000), 100)
        self.assertEqual(attempt.remaining_timeout(deadlines, 1800, cleanup=True, now=1000), 1800)
        self.assertEqual(attempt.remaining_timeout(deadlines, 1800, cleanup=True, now=2100), 1800)
        with self.assertRaises(attempt.PolicyError):
            attempt.remaining_timeout(deadlines, 1800, now=1100)
        for bad in (True, float("nan"), float("inf"), "2000"):
            with self.assertRaises(attempt.PolicyError):
                attempt.remaining_timeout({"deadline": bad}, 30, cleanup=True, now=1000)

    def test_timed_out_process_group_cannot_finish_child_write(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child"
            child = "import time;from pathlib import Path;time.sleep(.4);Path(" + repr(str(marker)) + ").touch()"
            parent = "import subprocess,time;subprocess.Popen([" + repr(sys.executable) + ",'-c'," + repr(child) + "]);time.sleep(10)"
            with self.assertRaisesRegex(attempt.PolicyError, "command_timed_out"):
                attempt.command([sys.executable, "-c", parent], timeout=.1)
            time.sleep(.5)
            self.assertFalse(marker.exists())

    def test_failed_native_command_keeps_bounded_private_output_without_exposing_it_in_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            code = "import sys;sys.stderr.write('fixture-secret-error');sys.stdout.write('x'*1200000);sys.exit(42)"
            for name in ("terraform-command-error.json", "container-command-error.json", "runtime-command-error.json", "runtime-restore-error.json"):
                with self.subTest(name=name):
                    report = Path(temp) / name
                    with self.assertRaisesRegex(attempt.PolicyError, "^command_failed$"):
                        attempt.command([sys.executable, "-c", code], failure_report=report)
                    value = json.loads(report.read_text())
                    self.assertEqual(value["exit_code"], 42)
                    self.assertEqual(value["stderr"], "fixture-secret-error")
                    self.assertEqual(len(value["stdout"]), 1024 * 1024)
                    self.assertTrue(value["truncated"])
                    self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_interrupted_native_command_preserves_private_failure_and_does_not_follow_report_links(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "terraform-command-error.json"
            code = "import sys,time;sys.stderr.write('private-partial-error');sys.stderr.flush();time.sleep(10)"
            with self.assertRaisesRegex(attempt.PolicyError, "command_timed_out"):
                attempt.command([sys.executable, "-c", code], timeout=.2, failure_report=report)
            self.assertEqual(json.loads(report.read_text())["stderr"], "private-partial-error")
            outside = Path(temp) / "unrelated"; outside.write_text("preserve")
            report.unlink(); report.symlink_to(outside)
            with self.assertRaises(attempt.PolicyError):
                attempt.command([sys.executable, "-c", "import sys;sys.exit(1)"], failure_report=report)
            self.assertEqual(outside.read_text(), "preserve")


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "git and ssh-keygen required")
class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        (self.repo / "examples/showcase").mkdir(parents=True)
        (self.repo / "examples/showcase/terraform.tfvars.example").write_text((attempt.ROOT / "examples/showcase/terraform.tfvars.example").read_text())
        (self.repo / "main.tf").write_text("# committed\n")
        (self.repo / "variables.tf").write_text("# schema\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.batch = self.root / "batch"

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True, text=True).stdout

    def prepare(self):
        return attempt.prepare(config(), "aws", "as7-fixture01", self.batch, self.revision, self.repo)

    def test_exact_committed_export_fresh_key_no_operator_state_or_credentials_and_private_modes(self):
        (self.repo / "main.tf").write_text("# uncommitted change\n")
        (self.repo / "terraform.tfvars").write_text("private-operator-value")
        (self.repo / "terraform.tfstate").write_text("private-operator-state")
        record = self.prepare()
        run = self.batch / "attempts/as7-fixture01"
        self.assertEqual((run / "repo/main.tf").read_text(), "# committed\n")
        self.assertFalse((run / "repo/terraform.tfstate").exists())
        self.assertFalse((run / "repo/terraform.tfvars").exists())
        self.assertFalse((run / "repo/.git").exists())
        self.assertEqual(record["source_revision"], self.revision)
        self.assertFalse(record["spend_reserved"])
        self.assertFalse((self.batch / "reservations.json").exists())
        self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in run.rglob("*") if path.is_file() and path.suffix != ".pub"))
        self.assertEqual(attempt.verify_prepared(run, config(), "aws"), record)
        with self.assertRaisesRegex(attempt.PolicyError, "already_exists"):
            self.prepare()

    def test_modified_source_inputs_settings_key_or_extra_override_refuse_recovery(self):
        self.prepare()
        run = self.batch / "attempts/as7-fixture01"
        for filename, content in (("repo/main.tf", b"# modified"), ("repo/terraform.tfvars.json", b"{}"),
                                  ("repo/override.tf", b"# foreign"), ("repo/.ssh/id_ed25519_agent_stack.pub", b"ssh-ed25519 foreign")):
            path = run / filename
            previous = path.read_bytes() if path.exists() else None
            path.write_bytes(content)
            with self.subTest(filename=filename), self.assertRaises(attempt.PolicyError):
                attempt.verify_prepared(run, config(), "aws")
            path.unlink() if previous is None else path.write_bytes(previous)
        changed = config()
        changed["providers"]["aws"]["region"] = "other-region"
        with self.assertRaises(attempt.PolicyError):
            attempt.verify_prepared(run, changed, "aws")
        changed = config()
        changed["pins"]["codex_release"] = "0.146.0"
        with self.assertRaises(attempt.PolicyError):
            attempt.verify_prepared(run, changed, "aws")

    def test_links_in_committed_source_are_refused_without_creating_a_run(self):
        (self.repo / "linked").symlink_to("/etc/passwd")
        self.git("add", "linked")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "link")
        with self.assertRaises(attempt.PolicyError):
            attempt.prepare(config(), "aws", "as7-fixture01", self.batch, repo=self.repo)
        self.assertFalse(self.batch.exists())

    def test_native_state_lock_metadata_is_private_and_cannot_allow_other_overrides(self):
        self.prepare()
        run = self.batch / "attempts/as7-fixture01"
        lock = run / "repo/.terraform.tfstate.lock.info"
        lock.write_text('{"Operation":"OperationTypeApply"}')
        lock.chmod(0o600)
        attempt.verify_prepared(run, config(), "aws")
        lock.chmod(0o644)
        with self.assertRaisesRegex(attempt.PolicyError, "private_state_lock_required"):
            attempt.verify_prepared(run, config(), "aws")
        lock.unlink()
        lock.symlink_to(run / "inputs.json")
        with self.assertRaises(attempt.PolicyError):
            attempt.verify_prepared(run, config(), "aws")
        lock.unlink()
        (run / "repo/.terraform.tfstate.lock.info.override.tf").write_text("foreign")
        with self.assertRaisesRegex(attempt.PolicyError, "unexpected_prepared_file"):
            attempt.verify_prepared(run, config(), "aws")


class PasswordTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required")
    def test_real_hash_reads_reference_once_stdin_only_fresh_salt_and_no_plaintext_output(self):
        password = "fake-test-password:$()'with-spaces"
        with patch.object(attempt, "command", wraps=attempt.command) as execute, patch.object(attempt, "read_secret") as reader:
            # Injecting the reader exercises the exact helper without op/login.
            reader.return_value = password
            first = attempt.workspace_password_hash(REF, secret_reader=reader)
            reader.assert_called_once_with(REF)
            self.assertNotIn(password, str(execute.call_args.args))
            self.assertEqual(execute.call_args.kwargs["input"], password.encode() + b"\n")
        second = attempt.workspace_password_hash(REF, secret_reader=lambda _: password)
        self.assertNotEqual(first, second)
        salt = first.split("$")[2]
        expected = attempt.command(["openssl", "passwd", "-6", "-salt", salt, "-stdin"], input=password.encode() + b"\n").decode().strip()
        self.assertEqual(first, expected)
        self.assertNotIn(password, first)

    def test_newline_nul_short_and_overlong_passwords_refused_before_hashing(self):
        for password in ("short", "a" * 129, "a" * 14 + "\n", "a" * 14 + "\x00"):
            with patch.object(attempt, "command") as execute, self.assertRaises(attempt.PolicyError):
                attempt.workspace_password_hash(REF, secret_reader=lambda _: password)
            execute.assert_not_called()


class EntrypointTest(unittest.TestCase):
    def entrypoint(self, password="", hashed=""):
        prefix = (attempt.ROOT / "modules/common/templates/runtime/workspace-entrypoint.sh.tpl").read_text().split("authorized_keys_b64=")[0]
        prefix = prefix.replace("$${", "${")
        functions = '''
id() { if [ "$1" = -u ] || [ "$1" = -g ]; then echo 1000; fi; }
getent() { echo 'user:x:1000:1000::/home/user:/bin/bash'; }
chown() { :; }
chmod() { :; }
install() { :; }
chpasswd() { printf '%s' "$*" > "$TEST_COMMAND"; cat > "$TEST_INPUT"; }
'''
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = dict(os.environ, WORKSPACE_USERNAME="user", WORKSPACE_PASSWORD=password, WORKSPACE_PASSWORD_HASH=hashed,
                       TEST_COMMAND=str(root / "command"), TEST_INPUT=str(root / "input"))
            result = subprocess.run(["bash"], input=functions + prefix + '\nenv\n', env=env, capture_output=True, text=True)
            return result, (root / "command").read_text() if (root / "command").exists() else None, (root / "input").read_text() if (root / "input").exists() else None

    def test_hash_and_legacy_password_authentication_work_without_secret_environment(self):
        hashed = "$6$fixturesalt12345$" + "a" * 86
        result, command, value = self.entrypoint(hashed=hashed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(command, "--encrypted")
        self.assertEqual(value, "user:" + hashed + "\n")
        self.assertNotIn("WORKSPACE_PASSWORD", result.stdout)
        result, command, value = self.entrypoint(password="fake-legacy-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(command, "")
        self.assertEqual(value, "user:fake-legacy-password\n")
        self.assertNotIn("WORKSPACE_PASSWORD", result.stdout)

    def test_missing_conflicting_or_invalid_hashes_fail_before_account_update_without_values(self):
        for password, hashed in (("", ""), ("fake-password", "$6$fixturesalt12345$" + "a" * 86),
                                 ("", "plaintext-private-placeholder"), ("", "$1$fixturesalt$" + "a" * 86)):
            result, command, value = self.entrypoint(password, hashed)
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(command)
            self.assertIsNone(value)
            self.assertNotIn("plaintext-private-placeholder", result.stderr)
