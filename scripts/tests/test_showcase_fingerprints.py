import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import showcase_fingerprints as fp
import showcase_contents as observed
from showcase_cleanup import scope, receipt as cleanup_receipt
from showcase_policy import PolicyError
from test_showcase_policy import config

ROOT = Path(__file__).resolve().parents[2]


def contents(binding=None):
    return {"schema": 2, "status": "resolved_contents_verified", "boot_image": "aws:ami-fixture",
        "binding": binding or {"run_id": "as7-fingerprint01", "source_revision": "a" * 40, "checksum": "b" * 64, "vm_id": "vm-fixture"},
        "images": {role: {"image_id": "sha256:" + char * 64, "layers": ["sha256:" + char * 64], "os": "linux", "architecture": "amd64", "variant": ""}
                   for role, char in zip(("openclaw", "hermes", "workspace"), "123")},
        "containers": {role: {"container_id": char * 64, "image_id": "sha256:" + char * 64}
                       for role, char in zip(("openclaw", "hermes", "workspace"), "123")},
        "hermes_repair_sha256": "4" * 64, "host": {"packages_sha256": "5" * 64, "kernel": "6.1.0-fixture", "os_release_sha256": "6" * 64,
            "tools": {name: "7" * 64 for name in ("/usr/bin/docker", "/usr/bin/tailscale", "/usr/sbin/sshd")}},
        "workspace_security": {"profile": "agent-stack-workspace-codex", "kernel_enforcing": True, "privileged": False,
            "cap_add": [], "no_new_privileges": True, **{key: "8" * 64 for key in
            ("installer_sha256", "bundle_sha256", "seccomp_file_sha256", "loaded_seccomp_sha256", "apparmor_sha256", "parser_sha256")}}}


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.recipe = {"schema": 1, "provider": "aws", "runtime": "a" * 64, "deployment": "b" * 64}
        self.components = fp.context(ROOT, self.config, "aws", self.recipe, contents())
        self.origin = {"provider": "aws", "scope": scope(self.config, "aws"), "run_id": "as7-fingerprint01", "source_revision": "a" * 40}
        self.checks = {}
        def add(name):
            for dep in fp.SPECS[name][1]:
                if dep not in self.checks: add(dep)
            self.checks[name] = fp.proof(ROOT, name, self.components, {"status": "pass", "fixture": name}, self.origin,
                                       {key: self.checks[key] for key in fp.SPECS[name][1]})
        add("light"); add("resolved_contents")
        receipt = cleanup_receipt({"scope": self.origin["scope"], "run_id": self.origin["run_id"], "identity": "verified",
            "resources": [], "unknown_resources": [], "nodes": [], "cloud_queries": 8, "tailnet_queries": 1, "verified_at": 100},
            {"scope": self.origin["scope"], "run_id": self.origin["run_id"], "captured_at": 99, "resources": [], "tailnet": {"devices": []}}, True)
        self.checks["verified_cleanup"] = fp.proof(ROOT, "verified_cleanup", self.components, receipt, self.origin, {})
        self.bundle = {"schema": 1, "mode": "light", "recipe": self.recipe, "cleanup": "verified", "checks": self.checks}

    def evaluate(self, **kwargs):
        return fp.evaluate(kwargs.get("root", ROOT), kwargs.get("bundle", self.bundle), kwargs.get("components", self.components),
                           "aws", scope(self.config, "aws"))

    def test_proven_contents_carry_across_commits_not_run_names(self):
        result = self.evaluate()
        self.assertEqual(result["groups"]["vm_and_containers"]["status"], "carry")
        manifest = contents(); manifest["binding"]["source_revision"] = "f" * 40; manifest["binding"]["run_id"] = "as7-newtrial02"
        manifest["binding"]["vm_id"] = "new-vm"; manifest["containers"]["hermes"]["container_id"] = "9" * 64
        components = fp.context(ROOT, self.config, "aws", self.recipe, manifest)
        self.assertEqual(components, self.components)

    def test_missing_resolved_contents_never_carried_from_recipe(self):
        components = fp.context(ROOT, self.config, "aws", self.recipe)
        self.assertEqual(self.evaluate(components=components)["groups"]["vm_and_containers"]["status"], "reproof")

    def test_mutable_image_contents_invalidate_same_recipe(self):
        manifest = contents(); manifest["images"]["workspace"]["layers"] = ["sha256:" + "f" * 64]
        components = fp.context(ROOT, self.config, "aws", self.recipe, manifest)
        result = self.evaluate(components=components)
        self.assertEqual(result["checks"]["light"]["reason"], "fingerprint_inputs_changed")
        self.assertEqual(result["checks"]["apply"]["status"] if "apply" in result["checks"] else self.checks["apply"]["status"], "pass")

    def test_hermes_writable_repair_invalidates_unchanged_image_ids(self):
        manifest = contents(); manifest["hermes_repair_sha256"] = "e" * 64
        result = self.evaluate(components=fp.context(ROOT, self.config, "aws", self.recipe, manifest))
        self.assertEqual(result["groups"]["vm_and_containers"]["status"], "reproof")

    def test_host_package_change_requires_deployment_reproof(self):
        manifest = contents(); manifest["host"]["packages_sha256"] = "e" * 64
        self.assertEqual(self.evaluate(components=fp.context(ROOT, self.config, "aws", self.recipe, manifest))["groups"]["vm_and_containers"]["status"], "reproof")

    def test_resolved_boot_image_change_invalidates_even_same_packages(self):
        manifest = contents(); manifest["boot_image"] = "aws:ami-new-image"
        self.assertEqual(self.evaluate(components=fp.context(ROOT, self.config, "aws", self.recipe, manifest))["groups"]["vm_and_containers"]["status"], "reproof")

    def test_loaded_workspace_policy_change_requires_reproof(self):
        manifest = contents(); manifest["workspace_security"]["loaded_seccomp_sha256"] = "e" * 64
        self.assertEqual(self.evaluate(components=fp.context(ROOT, self.config, "aws", self.recipe, manifest))["groups"]["vm_and_containers"]["status"], "reproof")

    def test_unsafe_or_missing_workspace_security_cannot_carry(self):
        for key, bad in (("privileged", True), ("kernel_enforcing", False), ("no_new_privileges", False),
                         ("cap_add", ["SYS_ADMIN"]), ("profile", "unconfined"), ("loaded_seccomp_sha256", None)):
            with self.subTest(key=key):
                value = contents(); value["workspace_security"][key] = bad
                with self.assertRaisesRegex(PolicyError, "workspace_security_unverified"): fp.validate_contents(value)

    def test_docs_edit_preserves_runtime_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in fp.verifier_files(ROOT, "light"):
                p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes((ROOT / name).read_bytes())
            (root / "README.md").write_text("changed docs")
            self.assertEqual(self.evaluate(root=root)["groups"]["vm_and_containers"]["status"], "carry")
            p = root / "scripts/showcase_light.py"; p.write_text(p.read_text() + "\n# verifier fixed\n")
            self.assertEqual(self.evaluate(root=root)["checks"]["light"]["reason"], "fingerprint_verifier_changed")

    def copied_verifiers(self, stages):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in set().union(*(set(fp.verifier_files(ROOT, stage)) for stage in stages)):
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        return root

    def test_browser_dependencies_are_limited_to_browser_stages(self):
        stages = {name for name in fp.SPECS if 'scripts/showcase_browser.py' in fp.verifier_files(ROOT, name)}
        self.assertEqual(stages, set(fp.BROWSER_STAGES))

    def test_full_runtime_adapter_is_not_a_light_or_bootstrap_dependency(self):
        for stage in fp.NON_MODEL_STAGES:
            paths = fp.verifier_files(ROOT, stage)
            self.assertNotIn('scripts/showcase_model_session.py', paths)
            self.assertNotIn('scripts/showcase_runtime_lifecycle.py', paths)
        for stage in ('model_channel', 'openclaw_activation', 'doctors', 'model_channel_closed'):
            self.assertIn('scripts/showcase_runtime_lifecycle.py', fp.verifier_files(ROOT, stage))

    def test_runtime_fix_changes_full_verifiers_without_invalidating_light(self):
        stages = ('light', 'resolved_contents', 'bootstrap', 'workspace_sandbox', 'openclaw_activation', 'doctors')
        root = self.copied_verifiers(stages); before = {s: fp.verifier(root, s) for s in stages}
        for name in ('showcase_runtime_lifecycle', 'showcase_model_session'):
            path = root / 'scripts' / (name + '.py'); path.write_text(path.read_text() + '\n# changed runtime verifier\n')
        for stage in stages:
            with self.subTest(stage=stage):
                self.assertEqual(fp.verifier(root, stage) == before[stage], stage in fp.NON_MODEL_STAGES)

    def test_model_import_outside_adapter_or_changed_symbols_cannot_silently_carry(self):
        for replacement in ('from showcase_model_session import ModelSession, unknown_symbol', None):
            root = self.copied_verifiers(('light', 'model_channel', 'openclaw_activation'))
            path = root / 'scripts/showcase_remote.py'; source = path.read_text()
            if replacement: source = source.replace('from showcase_model_session import ModelSession', replacement)
            else: source += '\nimport showcase_model_session\n'
            path.write_text(source)
            with self.assertRaisesRegex(PolicyError, '^fingerprint_(deferred_dependency_changed|model_dependency_outside_stage)$'):
                fp.verifier_files(root, 'light')

    def test_browser_fix_preserves_unrelated_verifiers_but_changes_both_browser_checks(self):
        stages = ('apply', 'light', 'doctors', 'before_reboot', 'after_reboot')
        root = self.copied_verifiers(stages)
        before = {stage:fp.verifier(root, stage) for stage in stages}
        browser = root / 'scripts/showcase_browser.py'
        browser.write_text(browser.read_text() + '\n# stricter browser verification\n')
        for stage in stages:
            with self.subTest(stage=stage):
                self.assertEqual(fp.verifier(root, stage) == before[stage], stage not in fp.BROWSER_STAGES)

    def test_new_peer_import_outside_deferred_callback_is_rejected(self):
        root = self.copied_verifiers(('light', 'before_reboot'))
        peer = root / 'scripts/showcase_local_peer.py'
        peer.write_text(peer.read_text() + '\ndef unexpected():\n    import showcase_peer\n')
        with self.assertRaisesRegex(PolicyError, '^fingerprint_browser_dependency_outside_stage$'):
            fp.verifier_files(root, 'light')

    def test_changed_deferred_import_symbols_require_explicit_dependency_review(self):
        root = self.copied_verifiers(('light',))
        peer = root / 'scripts/showcase_local_peer.py'
        text = peer.read_text(); self.assertIn('from showcase_peer import LOADER, payload_valid', text)
        peer.write_text(text.replace('from showcase_peer import LOADER, payload_valid',
                                     'from showcase_peer import LOADER, payload_valid, new_callback'))
        with self.assertRaisesRegex(PolicyError, '^fingerprint_deferred_dependency_changed$'):
            fp.verifier_files(root, 'light')

    def test_added_local_dependency_is_hashed_and_missing_dependency_is_rejected(self):
        root = self.copied_verifiers(('light',))
        light = root / 'scripts/showcase_light.py'
        light.write_text(light.read_text() + '\nimport showcase_new_verifier\n')
        with self.assertRaisesRegex(PolicyError, '^fingerprint_source_missing$'):
            fp.verifier_files(root, 'light')
        dependency = root / 'scripts/showcase_new_verifier.py'; dependency.write_text('RULE = 1\n')
        before = fp.verifier(root, 'light'); dependency.write_text('RULE = 2\n')
        self.assertNotEqual(fp.verifier(root, 'light'), before)

    def test_earlier_false_pass_or_missing_dependency_never_carried(self):
        for mutate in (lambda b: b["checks"].pop("apply"), lambda b: b["checks"]["apply"].update(status="unverified"),
                       lambda b: b["checks"]["light"]["evidence"].update(status="fail")):
            bundle = copy.deepcopy(self.bundle); mutate(bundle)
            self.assertEqual(self.evaluate(bundle=bundle)["groups"]["vm_and_containers"]["status"], "reproof")

    def test_forged_dependency_binding_is_rejected(self):
        bundle = copy.deepcopy(self.bundle); row = bundle["checks"]["light"]
        row["dependencies"]["apply"] = "f" * 64
        row["proof_sha256"] = fp.hashed({k: v for k, v in row.items() if k != "proof_sha256"})
        self.assertEqual(self.evaluate(bundle=bundle)["checks"]["light"]["reason"], "fingerprint_dependencies_changed")

    def test_scope_and_verifier_change_require_reproof(self):
        bundle = copy.deepcopy(self.bundle); row = bundle["checks"]["light"]
        row["origin"]["scope"]["account_id"] = "another-account"
        row["proof_sha256"] = fp.hashed({k: v for k, v in row.items() if k != "proof_sha256"})
        self.assertEqual(self.evaluate(bundle=bundle)["checks"]["light"]["reason"], "fingerprint_scope_changed")

    def test_legacy_green_without_fingerprints_requires_baseline(self):
        self.assertEqual(self.evaluate(bundle={"mode": "light", "schema": 1, "checks": {}})["groups"]["vm_and_containers"]["status"], "reproof")

    def test_unclosed_original_trial_cannot_supply_carried_proof(self):
        bundle = copy.deepcopy(self.bundle); bundle["cleanup"] = "uncertain"
        self.assertEqual(self.evaluate(bundle=bundle)["checks"]["light"]["reason"], "fingerprint_original_cleanup_unverified")

    def test_native_cleanup_counts_carry_but_boolean_zero_wrong_scope_or_remaining_ids_do_not(self):
        self.assertEqual(self.evaluate()["groups"]["vm_and_containers"]["status"], "carry")
        for changes in ({"cloud_queries_succeeded": True}, {"tailnet_queries_succeeded": 0},
                        {"account_id": "another-account"}, {"remaining_ids": ["still-present"]},
                        {"terraform_state_verified_empty": False}):
            bundle = copy.deepcopy(self.bundle); row = bundle["checks"]["verified_cleanup"]
            row["evidence"].update(changes); row["evidence_sha256"] = fp.hashed(row["evidence"])
            row["proof_sha256"] = fp.hashed({k: v for k, v in row.items() if k != "proof_sha256"})
            self.assertEqual(self.evaluate(bundle=bundle)["checks"]["light"]["reason"], "fingerprint_original_cleanup_unverified")

    def test_cleanup_is_always_fresh_and_has_preserved_evidence(self):
        self.checks["verified_cleanup"] = fp.proof(ROOT, "verified_cleanup", self.components, {"native_absence": "verified"}, self.origin, {})
        result = self.evaluate(bundle={**self.bundle, "mode": "full"})
        self.assertEqual(result["checks"]["verified_cleanup"]["reason"], "fresh_cleanup_required")

    def test_ephemeral_peer_and_budget_do_not_change_app_or_access_inputs(self):
        cfg = copy.deepcopy(self.config); cfg["tailnet"]["runner_dns"] = "newpeer.fixture.ts.net"; cfg["budget_usd"] = "35"
        self.assertEqual(fp.context(ROOT, cfg, "aws", self.recipe, contents()), self.components)

    def test_plan_fingerprint_requires_complete_normalized_known_outputs(self):
        plan = {"planned_values": {"outputs": {"deployment_input_fingerprints": {"value": self.recipe}}}}
        self.assertEqual(fp.recipe_from_plan(plan, "aws"), self.recipe)
        for value in ({}, {**self.recipe, "provider": "gcp"}, {**self.recipe, "deployment": None}):
            plan["planned_values"]["outputs"]["deployment_input_fingerprints"]["value"] = value
            with self.assertRaises(PolicyError): fp.recipe_from_plan(plan, "aws")

    def test_recipe_only_or_mismatched_observation_refused(self):
        for value in ({"images": contents()["images"]}, {**contents(), "hermes_repair_sha256": None}):
            with self.assertRaises(PolicyError): fp.validate_contents(value)
        with self.assertRaises(PolicyError): fp.validate_contents(contents(), {**contents()["binding"], "vm_id": "other-vm"})

    def test_host_file_hash_streams_full_contents_on_python_before_file_digest(self):
        import hashlib
        data = b"resolved-file-bytes" * 150000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture"; path.write_bytes(data)
            with patch.object(observed.hashlib, "file_digest", side_effect=AssertionError("unavailable on Python 3.10"), create=True):
                self.assertEqual(observed.file_hash(path), hashlib.sha256(data).hexdigest())
                path.write_bytes(b"")
                self.assertEqual(observed.file_hash(path), hashlib.sha256(b"").hexdigest())

    def test_resolved_tree_tracks_file_bytes_modes_and_links_but_not_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "web").mkdir(); file = root / "web/code.js"; file.write_text("before")
            first = observed.tree_hash(root, ["web"])
            import os
            os.utime(file, (1, 1)); self.assertEqual(observed.tree_hash(root, ["web"]), first)
            file.write_text("after"); self.assertNotEqual(observed.tree_hash(root, ["web"]), first)
            first = observed.tree_hash(root, ["web"]); file.chmod(0o755); self.assertNotEqual(observed.tree_hash(root, ["web"]), first)
            (root / "web/outside").symlink_to("/etc/passwd")
            with self.assertRaises(AssertionError): observed.tree_hash(root, ["web"])

    def test_remote_process_input_reaches_child_without_stdin_conflict(self):
        self.assertEqual(observed.run(sys.executable, "-B", "-I", "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
                                      input=b"credential-free-fixture"), b"credential-free-fixture")

    def test_every_named_controller_stage_has_declared_dependencies(self):
        names = set(fp.SPECS)
        for components, checks, verifiers in fp.SPECS.values():
            self.assertTrue(components); self.assertTrue(set(checks) <= names)


if __name__ == "__main__": unittest.main()
