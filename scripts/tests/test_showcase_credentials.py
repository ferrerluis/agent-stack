import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_credentials as credentials
from showcase_policy import secret_ref
from showcase_preflight import read_secret, PreflightError


class TemporaryCredentialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "auth"
        self.root.mkdir(mode=0o700)
        self.patch = patch.object(credentials, "AUTH_ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.file = self.root / "selected-token"
        self.file.write_text("fixture-private-value\n")
        self.file.chmod(0o600)
        self.ref = self.file.as_uri()

    def test_selected_file_without_op_or_content_diagnostic(self):
        with patch("showcase_preflight.subprocess.run") as run:
            self.assertTrue(secret_ref(self.ref))
            self.assertEqual(read_secret(self.ref), "fixture-private-value")
            run.assert_not_called()

    def test_paths_outside_authorized_root_and_ambiguous_urls_refused(self):
        for ref in (Path(self.tmp.name).as_uri(), "file:///etc/passwd", self.ref + "?x=1",
                    self.ref + "#x", self.ref.replace("selected-token", "../selected-token"),
                    self.ref.replace("selected-token", "%73elected-token"),
                    self.ref.replace("file:///", "file://localhost/")):
            self.assertFalse(secret_ref(ref), ref)

    def test_file_and_parent_symlinks_and_modes_refused(self):
        link = self.root / "link"
        link.symlink_to(self.file)
        parent = self.root / "link-dir"
        parent.symlink_to(self.root, target_is_directory=True)
        for ref in (link.as_uri(), (parent / self.file.name).as_uri()):
            with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
                read_secret(ref)
        for mode in (0o644, 0o400, 0o660):
            self.file.chmod(mode)
            with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
                read_secret(self.ref)
        self.file.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
            read_secret(self.ref)

    def test_fifo_empty_invalid_utf8_large_and_foreign_owner_refused(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo, mode=0o600)
        with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
            read_secret(fifo.as_uri())
        for data in (b"", b"\xff", b"x" * (credentials.MAX_SECRET + 1)):
            self.file.write_bytes(data)
            with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
                read_secret(self.ref)

    def test_oauth_refresh_once_per_fifty_minutes_and_failure_has_no_contents(self):
        ref = (self.root / "tailscale-access-token").as_uri()
        helper = self.root.parent / "scripts/tailscale-cli.py"
        helper.parent.mkdir()
        helper.write_text("# fixture")
        with patch.object(credentials, "_refreshed_at", None), patch.object(credentials.time, "monotonic", return_value=10) as clock, \
                patch.object(credentials.subprocess, "run") as run:
            run.return_value.returncode = 0
            credentials.refresh_access_token(ref)
            credentials.refresh_access_token(ref)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], [sys.executable, "-B", str(helper), "refresh"])
            clock.return_value = 3011
            run.return_value.returncode = 1
            with self.assertRaisesRegex(ValueError, "temporary_credential_refresh_failed"):
                credentials.refresh_access_token(ref)
        self.file.write_text("fixture-private-value")
        with patch.object(credentials.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(PreflightError, "temporary_credential_refused"):
                read_secret(self.ref)
