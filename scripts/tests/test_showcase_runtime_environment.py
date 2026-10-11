import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_runtime_environment as environment
import showcase_runtime_routes as routes
import test_showcase_runtime_routes as fixtures

ORIGINAL = (b"# owned fixture\nOPENAI_API_KEY=as7-model-gateway-not-active\n"
    b"ANTHROPIC_API_KEY=as7-model-gateway-not-active\nOPENAI_BASE_URL=https://api.openai.com/v1\n"
    b"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD='fixture-app-password'\nAPI_SERVER_KEY=fixture-server-key\nLANG=C.UTF-8\n")


class EnvironmentTest(unittest.TestCase):
    def make(self):
        if getattr(self, "files", None): self.files.doCleanups()
        self.files = fixtures.TransactionTest("runTest"); self.files.setUp(); self.addCleanup(self.files.doCleanups)
        self.tx = environment.EnvironmentTransaction(self.files.transaction)
        self.tx.target.write_bytes(ORIGINAL); self.tx.target.chmod(0o640)
        return self.tx

    def test_both_wires_remove_only_inactive_keys_and_keep_real_app_authentication(self):
        for provider in ("openai", "anthropic"):
            route = fixtures.route("hermes", provider)
            after = environment.compile_environment(ORIGINAL, route)
            self.assertIn(b"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD='fixture-app-password'\n", after)
            self.assertIn(b"API_SERVER_KEY=fixture-server-key\nLANG=C.UTF-8\n", after)
            self.assertNotIn(environment.INACTIVE.encode(), after)
            self.assertNotIn(b"_API_KEY=", after); self.assertNotIn(b"api.openai.com", after)
            self.assertNotIn(fixtures.CAPABILITY.encode(), after)
            self.assertEqual(environment.assignments(after)[0]["OPENAI_BASE_URL"], route["base_url"] + ("/v1" if provider == "openai" else ""))
            environment.selected_environment(after, route)

    def test_real_unknown_credentials_cloud_identity_proxy_external_auth_and_expansion_refuse(self):
        for line in ("ANTHROPIC_API_KEY=fixture-real-provider-key", "CUSTOM_PROVIDER_API_KEY=fixture-secret",
                     "GITHUB_TOKEN=fixture-secret", "OP_SERVICE_ACCOUNT_TOKEN=fixture-secret", "AWS_PROFILE=personal",
                     "GOOGLE_APPLICATION_CREDENTIALS=/private/personal.json", "HERMES_MANAGED_DIR=/private/global",
                     "HTTPS_PROXY=https://proxy.invalid", "LD_PRELOAD=/private/shim.so", "PYTHONPATH=/private/code",
                     "HERMES_HOME=/private/personal", "OPENAI_API_KEY=${PERSONAL_API_KEY}", "OPENAI_BASE_URL=https://host.invalid/?token=secret"):
            raw = line.encode() + b"\n"
            with self.subTest(name=line.split("=", 1)[0]), self.assertRaises(ValueError):
                environment.compile_environment(raw, fixtures.route("hermes"))
        result = environment.compile_environment(b"HOME=/opt/data\nHERMES_HOME=/opt/data\n", fixtures.route("hermes"))
        self.assertEqual(set(environment.assignments(result)[0]), {"OPENAI_BASE_URL"})

    def test_duplicate_export_multiline_controls_no_terminal_newline_wrong_role_and_foreign_selected_route_refuse(self):
        for raw in (b"A=1\nA=2\n", b"export OPENAI_API_KEY=x\n", b"A=x", b"A=x\r\n", b"A=x\x00\n",
                    b"A='first\nsecond'\n", b"a=x\n", b"\xff\n", b"A=" + b"x" * routes.LIMIT + b"\n"):
            with self.subTest(raw_len=len(raw)), self.assertRaises((ValueError, UnicodeError)):
                environment.compile_environment(raw, fixtures.route("hermes"))
        with self.assertRaises(ValueError): environment.compile_environment(ORIGINAL, fixtures.route())
        for raw in (b"OPENAI_BASE_URL=https://api.openai.com/v1\n", b"OPENAI_BASE_URL=http://172.17.0.1:23456/hermes/v1\nOPENAI_API_KEY=\n"):
            with self.assertRaises(ValueError): environment.selected_environment(raw, fixtures.route("hermes"))

    def test_real_private_snapshot_precedes_replacement_and_exact_original_bytes_and_mode_restore(self):
        self.make(); before = self.tx.target.read_bytes()
        installed = self.tx.install(fixtures.binding(), fixtures.route("hermes"))
        self.assertEqual(installed, hashlib.sha256(self.tx.target.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(self.tx.record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tx.target.stat().st_mode), 0o600)
        self.assertEqual(routes.decoded(self.tx.record.read_bytes())["original_sha256"], routes.sha(before))
        self.assertEqual(self.tx.restore(fixtures.binding()), routes.sha(before))
        self.assertEqual(self.tx.target.read_bytes(), before); self.assertEqual(stat.S_IMODE(self.tx.target.stat().st_mode), 0o640)
        self.assertFalse(self.tx.record.exists())

    def test_environment_reads_may_change_access_time_without_becoming_operator_edits(self):
        self.make(); checked = routes.checked_path; calls = []
        def reading(path, **kwargs):
            info = checked(path, **kwargs)
            if Path(path) == self.tx.target:
                calls.append(path); return fixtures.changed_access_time(info, len(calls))
            return info
        with patch.object(routes, "checked_path", side_effect=reading):
            installed = self.tx.install(fixtures.binding(), fixtures.route("hermes"))
        self.assertEqual(installed, routes.sha(self.tx.target.read_bytes())); self.assertGreaterEqual(len(calls), 2)
        self.tx.restore(fixtures.binding()); self.assertEqual(self.tx.target.read_bytes(), ORIGINAL)

    def test_same_environment_bytes_with_changed_permissions_identity_or_write_times_refuse(self):
        for kind in ("permissions", "replacement", "rewrite"):
            self.make(); atomic = routes.atomic_write; info = self.tx.target.stat()
            def edit(path, *args, **kwargs):
                result = atomic(path, *args, **kwargs)
                if path == self.tx.record:
                    if kind == "permissions": self.tx.target.chmod(0o600)
                    elif kind == "rewrite": self.tx.target.write_bytes(ORIGINAL)
                    else:
                        replacement = self.files.root / "same-bytes-replacement"
                        replacement.write_bytes(ORIGINAL); replacement.chmod(0o640)
                        replacement.replace(self.tx.target)
                return result
            with patch.object(routes, "atomic_write", side_effect=edit), self.assertRaises(ValueError):
                self.tx.install(fixtures.binding(), fixtures.route("hermes"))
            self.assertEqual(self.tx.target.read_bytes(), ORIGINAL); self.assertTrue(self.tx.record.exists())
            if kind == "permissions": self.assertEqual(stat.S_IMODE(self.tx.target.stat().st_mode), 0o600)
            elif kind == "replacement": self.assertNotEqual(self.tx.target.stat().st_ino, info.st_ino)
            else: self.assertNotEqual(self.tx.target.stat().st_mtime_ns, info.st_mtime_ns)

    def test_install_failure_after_journal_is_recoverable_and_cannot_replay(self):
        self.make(); atomic = routes.atomic_write
        def crash(path, *args, **kwargs):
            if path == self.tx.target: raise OSError("fixture crash")
            return atomic(path, *args, **kwargs)
        with patch.object(routes, "atomic_write", side_effect=crash), self.assertRaises(OSError):
            self.tx.install(fixtures.binding(), fixtures.route("hermes"))
        self.assertTrue(self.tx.record.exists()); self.assertEqual(self.tx.target.read_bytes(), ORIGINAL)
        with self.assertRaises(ValueError): self.tx.install(fixtures.binding(), fixtures.route("hermes"))
        self.tx.restore(fixtures.binding()); self.assertEqual(self.tx.target.read_bytes(), ORIGINAL)

    def test_failed_restore_is_resumable_without_overwriting_foreign_file(self):
        self.make(); self.tx.install(fixtures.binding(), fixtures.route("hermes")); atomic = routes.atomic_write
        def lost_restore(path, *args, **kwargs):
            value = atomic(path, *args, **kwargs)
            if path == self.tx.target: raise OSError("fixture lost response")
            return value
        with patch.object(routes, "atomic_write", side_effect=lost_restore), self.assertRaises(OSError): self.tx.restore(fixtures.binding())
        self.assertTrue(self.tx.record.exists()); self.assertEqual(self.tx.target.read_bytes(), ORIGINAL)
        self.tx.restore(fixtures.binding()); self.assertFalse(self.tx.record.exists())
        self.make(); self.tx.install(fixtures.binding(), fixtures.route("hermes")); self.tx.target.write_bytes(b"LANG=operator-change\n")
        with self.assertRaises(ValueError): self.tx.restore(fixtures.binding())
        self.assertEqual(self.tx.target.read_bytes(), b"LANG=operator-change\n"); self.assertTrue(self.tx.record.exists())

    def test_stale_binding_corrupt_snapshot_public_journal_and_shared_lock_refuse(self):
        self.make(); self.tx.install(fixtures.binding(), fixtures.route("hermes")); current = self.tx.target.read_bytes()
        for field, value in (("source_revision", "b" * 40), ("boot_id", "b" * 8 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 4 + "-" + "b" * 12), ("checksum", "b" * 64)):
            with self.assertRaises(ValueError): self.tx.restore(fixtures.binding() | {field: value})
        snapshot = routes.decoded(self.tx.record.read_bytes())
        for change in ({"schema": True}, {"kind": "other"}, {"original": "broken-base64"}, {"original_sha256": "b" * 64}, {"uid": True}):
            self.tx.record.write_bytes(routes.encoded(snapshot | change))
            with self.assertRaises((ValueError, TypeError)): self.tx.restore(fixtures.binding())
            self.assertEqual(self.tx.target.read_bytes(), current)
        self.tx.record.write_bytes(routes.encoded(snapshot)); self.tx.record.chmod(0o644)
        with self.assertRaises(ValueError): self.tx.restore(fixtures.binding())
        self.tx.record.chmod(0o600)
        with self.files.transaction.lock(), self.assertRaises(BlockingIOError): self.tx.restore(fixtures.binding())

    def test_symlink_hardlink_and_unrecoverable_snapshot_size_refuse_before_mutation(self):
        for kind in ("symlink", "hardlink", "large"):
            self.make()
            if kind == "large": self.tx.target.write_bytes(b"#" + b"x" * 210000 + b"\n")
            else:
                external = self.files.root / "external"; external.write_bytes(ORIGINAL); external.chmod(0o640)
                self.tx.target.unlink()
                if kind == "symlink": self.tx.target.symlink_to(external)
                else: os.link(external, self.tx.target)
            before = self.tx.target.read_bytes()
            with self.assertRaises(ValueError): self.tx.install(fixtures.binding(), fixtures.route("hermes"))
            self.assertEqual(self.tx.target.read_bytes(), before); self.assertFalse(self.tx.record.exists())
        self.make(); target = self.files.target()
        before = json.dumps({"operator_note": "x" * 210000}).encode(); target.write_bytes(before)
        with self.assertRaises(ValueError): self.files.transaction.install(fixtures.binding(), fixtures.route())
        self.assertEqual(target.read_bytes(), before); self.assertFalse(self.files.journal().exists())


if __name__ == "__main__": unittest.main()
