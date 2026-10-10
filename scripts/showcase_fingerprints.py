"""Layered acceptance proofs. Git revisions are provenance, not invalidation.

Only matching resolved inputs, verifier bytes, intact evidence and dependency
proofs carry forward. Launch gates and independent cleanup are never cached.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

from showcase_policy import require, PolicyError, validate_cleanup_receipt
from showcase_cleanup import private_read, private_write, scope

SCHEMA = 1
HEX = r"[a-f0-9]{64}"
COMMON = ("scripts/showcase_fingerprints.py", "scripts/showcase_contents.py", "scripts/showcase_controller.py",
          "scripts/showcase_policy.py", "scripts/showcase_cleanup.py", "scripts/showcase_attempt.py")
# (input components, prerequisite checks, verifier entry points). Imports from
# these files are hashed recursively; external implementations are represented
# by observed installed bytes/versions and the committed provider lock file.
SPECS = {
    "apply": (("deployment", "runtime"), (), ("showcase_terraform",)),
    "bootstrap": (("deployment", "runtime", "host"), ("apply",), ("showcase_remote",)),
    "containers": (("runtime", "images", "hermes_repair"), ("bootstrap",), ("test_containers", "showcase_container_probe")),
    "light": (("deployment", "runtime", "host", "images", "hermes_repair", "workspace_security"), ("apply",), ("showcase_light", "showcase_remote")),
    "resolved_contents": (("deployment", "runtime", "host", "images", "hermes_repair", "workspace_security"), ("apply",), ("showcase_contents", "showcase_remote")),
    "workspace_sandbox": (("runtime", "host", "images", "workspace_security"), ("bootstrap", "resolved_contents"), ("showcase_workspace_sandbox", "showcase_remote")),
    "model_channel": (("runtime", "images", "hermes_repair", "models"), ("bootstrap", "resolved_contents", "workspace_sandbox"), ("showcase_model_session",)),
    "openclaw_activation": (("runtime", "images", "models"), ("model_channel",), ("showcase_runtime_lifecycle", "showcase_runtime_routes")),
    "hermes_activation": (("runtime", "images", "hermes_repair", "models"), ("model_channel",), ("showcase_runtime_lifecycle", "showcase_runtime_routes")),
    "doctors": (("runtime", "host", "images", "hermes_repair", "models"), ("openclaw_activation", "hermes_activation"), ("showcase_remote", "showcase_runtime_probe", "showcase_runtime_evidence")),
    "doctor_evidence": (("runtime", "images", "hermes_repair", "models"), ("doctors",), ("showcase_runtime_evidence",)),
    "claude": (("runtime", "images", "models", "workspace_security"), ("doctor_evidence", "workspace_sandbox", "before_reboot"), ("showcase_remote", "showcase_model_session", "showcase_model_gateway", "showcase_task_evidence")),
    "codex": (("runtime", "images", "models", "workspace_security"), ("doctor_evidence", "workspace_sandbox", "before_reboot"), ("showcase_remote", "showcase_model_session", "showcase_model_gateway", "showcase_task_evidence")),
    "openclaw": (("runtime", "images", "models"), ("doctor_evidence",), ("showcase_runtime_task",)),
    "hermes": (("runtime", "images", "hermes_repair", "models"), ("doctor_evidence",), ("showcase_runtime_task",)),
    "before_reboot": (("runtime", "images", "hermes_repair", "access"), ("doctor_evidence",), ("showcase_remote", "showcase_peer", "showcase_browser")),
    "model_channel_closed": (("runtime", "images", "hermes_repair", "models"), ("model_channel",), ("showcase_model_session",)),
    "reboot": (("deployment", "runtime", "host", "images", "hermes_repair"), ("bootstrap", "model_channel_closed"), ("showcase_remote",)),
    "resolved_contents_after_reboot": (("deployment", "runtime", "host", "images", "hermes_repair", "workspace_security"), ("reboot",), ("showcase_contents", "showcase_remote")),
    "workspace_sandbox_after_reboot": (("runtime", "host", "images", "workspace_security"), ("reboot", "resolved_contents_after_reboot"), ("showcase_workspace_sandbox", "showcase_remote")),
    "model_channel_after_reboot": (("runtime", "images", "hermes_repair", "models"), ("reboot", "resolved_contents_after_reboot", "workspace_sandbox_after_reboot"), ("showcase_model_session",)),
    "openclaw_activation_after_reboot": (("runtime", "images", "models"), ("model_channel_after_reboot",), ("showcase_runtime_lifecycle", "showcase_runtime_routes")),
    "hermes_activation_after_reboot": (("runtime", "images", "hermes_repair", "models"), ("model_channel_after_reboot",), ("showcase_runtime_lifecycle", "showcase_runtime_routes")),
    "doctors_after_reboot": (("runtime", "host", "images", "hermes_repair", "models"), ("openclaw_activation_after_reboot", "hermes_activation_after_reboot"), ("showcase_remote", "showcase_runtime_probe", "showcase_runtime_evidence")),
    "doctor_evidence_after_reboot": (("runtime", "images", "hermes_repair", "models"), ("doctors_after_reboot",), ("showcase_runtime_evidence",)),
    "reboot_verified": (("deployment", "runtime", "host", "images", "hermes_repair"), ("reboot", "doctor_evidence_after_reboot"), ("showcase_remote",)),
    "after_reboot": (("runtime", "images", "hermes_repair", "access"), ("reboot_verified",), ("showcase_remote", "showcase_peer", "showcase_browser")),
    "model_channel_after_reboot_closed": (("runtime", "images", "hermes_repair", "models"), ("model_channel_after_reboot",), ("showcase_model_session",)),
    "documentation_and_linked_plans": (("documentation",), (), ()),
    "verified_cleanup": (("deployment",), (), ("showcase_terraform", "showcase_cleanup")),
}
GROUPS = {
    "vm_and_containers": ("light", "resolved_contents"),
    "runtime_doctors": ("doctor_evidence",),
    "workspace_api_key_tasks": ("claude", "codex", "model_channel_closed"),
    "authenticated_tailscale_urls": ("before_reboot", "after_reboot"),
    "reboot_persistence": ("reboot_verified", "resolved_contents_after_reboot", "model_channel_after_reboot_closed"),
    "documentation_and_linked_plans": ("documentation_and_linked_plans",),
    "verified_cleanup": ("verified_cleanup",),
}


def hashed(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def files_hash(root, paths):
    rows = {}
    for name in sorted(set(paths)):
        path = Path(root) / name
        require(path.is_file() and not path.is_symlink(), "fingerprint_source_missing")
        rows[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashed(rows)


def verifier_files(root, name):
    require(name in SPECS, "fingerprint_check_unknown")
    paths = set(COMMON)
    pending = ["scripts/" + p + ".py" for p in SPECS[name][2]]
    if name.startswith("doctor"):
        pending += ["skills/agent-stack-doctor/scripts/strict_health.py", "modules/common/runtime/hermes_health.py"]
    if name in ("claude", "codex"):
        pending += ["modules/common/runtime/workspace_cli_task.py"]
    # Include repo-local imports, even dependencies outside the named script.
    while pending:
        path = pending.pop()
        if path in paths: continue
        paths.add(path)
        source = Path(root) / path
        require(source.is_file() and not source.is_symlink(), "fingerprint_source_missing")
        for node in ast.walk(ast.parse(source.read_bytes())):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            for module in names:
                if not module: continue
                for candidate in ("scripts/" + module.replace(".", "/") + ".py", str(Path(path).parent / (module + ".py"))):
                    if (Path(root) / candidate).is_file() and candidate not in paths:
                        pending.append(candidate)
    return tuple(sorted(paths))


def verifier(root, name):
    return files_hash(root, verifier_files(root, name))


def recipe_from_plan(plan, provider):
    value = plan.get("planned_values", {}).get("outputs", {}).get("deployment_input_fingerprints", {}).get("value")
    require(isinstance(value, dict) and set(value) == {"schema", "provider", "runtime", "deployment"}
            and type(value["schema"]) is int and value["schema"] == SCHEMA and value["provider"] == provider
            and all(isinstance(value[k], str) and re.fullmatch(HEX, value[k]) for k in ("runtime", "deployment")),
            "normalized_deployment_fingerprint_required")
    return value


def validate_contents(value, binding=None):
    require(isinstance(value, dict) and set(value) == {"schema", "status", "binding", "containers", "images", "hermes_repair_sha256", "host", "boot_image", "workspace_security"}
            and type(value["schema"]) is int and value["schema"] == 2 and value["status"] == "resolved_contents_verified",
            "resolved_contents_unverified")
    origin = value["binding"]
    require(isinstance(origin, dict) and set(origin) == {"run_id", "source_revision", "checksum", "vm_id"}
            and re.fullmatch(r"as7-[a-z0-9-]{8,30}", origin["run_id"])
            and re.fullmatch(r"[a-f0-9]{40}", origin["source_revision"]) and re.fullmatch(HEX, origin["checksum"])
            and isinstance(origin["vm_id"], str) and origin["vm_id"], "resolved_contents_binding_invalid")
    if binding is not None: require(origin == binding, "resolved_contents_binding_invalid")
    require(set(value["images"]) == set(value["containers"]) == {"openclaw", "hermes", "workspace"}, "resolved_contents_unverified")
    for role, row in value["images"].items():
        require(isinstance(row, dict) and set(row) == {"image_id", "layers", "os", "architecture", "variant"}
                and re.fullmatch("sha256:" + HEX, row["image_id"]) and row["os"] == "linux"
                and row["architecture"] in ("amd64", "arm64") and isinstance(row["variant"], str)
                and isinstance(row["layers"], list) and row["layers"] and len(row["layers"]) <= 128
                and all(isinstance(layer, str) and re.fullmatch("sha256:" + HEX, layer) for layer in row["layers"]), "resolved_contents_unverified")
        container = value["containers"][role]
        require(set(container) == {"container_id", "image_id"} and re.fullmatch(HEX, container["container_id"])
                and container["image_id"] == row["image_id"], "resolved_contents_unverified")
    require(isinstance(value["boot_image"], str) and 0 < len(value["boot_image"]) <= 1024, "resolved_boot_image_unverified")
    host = value["host"]
    require(isinstance(host, dict) and set(host) == {"packages_sha256", "os_release_sha256", "kernel", "tools"}
            and isinstance(host["kernel"], str) and 0 < len(host["kernel"]) <= 256
            and isinstance(host["tools"], dict) and set(host["tools"]) == {"/usr/bin/docker", "/usr/bin/tailscale", "/usr/sbin/sshd"}
            and all(isinstance(v, str) and re.fullmatch(HEX, v) for v in [host["packages_sha256"], host["os_release_sha256"],
                value["hermes_repair_sha256"], *host["tools"].values()]), "resolved_contents_unverified")
    security = value["workspace_security"]
    hashes = {"installer_sha256", "bundle_sha256", "seccomp_file_sha256", "loaded_seccomp_sha256", "apparmor_sha256", "parser_sha256"}
    require(isinstance(security, dict) and set(security) == hashes | {"profile", "kernel_enforcing", "privileged", "cap_add", "no_new_privileges"}
            and security["profile"] == "agent-stack-workspace-codex" and security["kernel_enforcing"] is True
            and security["privileged"] is False and security["cap_add"] == [] and security["no_new_privileges"] is True
            and all(isinstance(security[key], str) and re.fullmatch(HEX, security[key]) for key in hashes), "workspace_security_unverified")
    return value


def context(root, config, provider, recipe, contents=None, documentation=None):
    require(recipe.get("provider") == provider, "fingerprint_provider_changed")
    result = {"deployment": hashed({"recipe": recipe["deployment"], "scope": scope(config, provider)}), "runtime": recipe["runtime"],
              "models": hashed({"models": config["models"], "funding": config.get("funding", {}).get("prepaid_caps_usd"), "credentials": {k: config["secret_refs"].get(k) for k in ("openai_api_key", "anthropic_api_key")}}),
              "access": hashed({"tailnet": {k: config["tailnet"].get(k) for k in ("name", "auth_key_ref")}, "ssh_cidr": config["runner"]["ssh_cidr"]})}
    if contents is not None:
        validate_contents(contents)
        result.update(images=hashed(contents["images"]), hermes_repair=contents["hermes_repair_sha256"], host=hashed({"boot_image": contents["boot_image"], "host": contents["host"]}))
        result["workspace_security"] = hashed(contents["workspace_security"])
    if documentation is not None:
        require(isinstance(documentation, dict) and documentation, "documentation_fingerprint_required")
        result["documentation"] = hashed({"files": files_hash(root, ("README.md", "examples/showcase/README.md")), "linked_plans": documentation})
    return result


def input_fingerprint(name, components):
    keys = SPECS[name][0]
    require(all(key in components and isinstance(components[key], str) and re.fullmatch(HEX, components[key]) for key in keys),
            "fingerprint_inputs_unresolved")
    return hashed({"schema": SCHEMA, "check": name, "inputs": {key: components[key] for key in keys}})


def proof(root, name, components, evidence, origin, dependencies):
    require(name in SPECS and set(dependencies) == set(SPECS[name][1]), "fingerprint_dependencies_unresolved")
    links = {}
    for key, value in dependencies.items():
        require(value.get("status") == "pass", "fingerprint_dependencies_unresolved")
        links[key] = hashed({"input": value["input_fingerprint"], "verifier": value["verifier_fingerprint"]})
    value = {"schema": SCHEMA, "name": name, "status": "pass", "input_fingerprint": input_fingerprint(name, components),
             "verifier_fingerprint": verifier(root, name), "dependencies": links, "evidence": evidence,
             "evidence_sha256": hashed(evidence), "origin": origin}
    value["proof_sha256"] = hashed(value)
    return value


def record_checks(root, record, config, recipe, observed):
    """Keep partial passes, without claiming a complete aggregate after failure."""
    checks = {}; mode = record["acceptance_mode"]
    origin = {key: record[key] for key in ("run_id", "source_revision", "provider", "scope")}
    for row in record["steps"]:
        name = row["name"]
        require(name in SPECS, "fingerprint_check_unknown")
        value = observed.get(name)
        saved = {"schema": SCHEMA, "name": name, "status": "unverified", "input_fingerprint": None,
                 "verifier_fingerprint": verifier(root, name), "evidence": value,
                 "evidence_sha256": hashed(value), "origin": origin, "dependencies": {}}
        if row["status"] == "pass" and row.get("proof_sha256") == hashed(value):
            try:
                manifest = observed.get("resolved_contents_after_reboot" if "after_reboot" in name or name == "reboot_verified" else "resolved_contents")
                components = context(root, config, record["provider"], recipe, manifest)
                saved = proof(root, name, components, value, origin, {key: checks.get(key, {}) for key in SPECS[name][1]})
            except (PolicyError, ValueError, TypeError, KeyError) as error:
                saved["reason"] = str(error) if isinstance(error, PolicyError) else "fingerprint_inputs_unresolved"
                pass  # A partial/missing contents baseline never becomes reusable.
        checks[name] = saved
        row["fingerprint"] = saved
    return {"schema": SCHEMA, "mode": mode, "recipe": recipe, "cleanup": record.get("cleanup"), "checks": checks}


def evaluate(root, bundle, components, provider, expected_scope):
    """Evaluate a retained bundle against the candidate's actual contents.

    Passing proofs may come from a different commit. Never infer matching image
    contents from unchanged recipes; caller must select an observed manifest.
    """
    result = {}; visiting = set()
    checks = bundle.get("checks", {}) if bundle.get("schema") == SCHEMA else {}
    def visit(name):
        if name in result: return result[name]
        reason = "missing_fingerprints"; row = checks.get(name, {})
        try:
            require(name != "verified_cleanup", "fresh_cleanup_required")
            require(name not in visiting, "fingerprint_dependency_cycle")
            visiting.add(name)
            require(row.get("schema") == SCHEMA and row.get("name") == name and row.get("status") == "pass", reason)
            require(row.get("proof_sha256") == hashed({k: v for k, v in row.items() if k != "proof_sha256"})
                    and row.get("evidence_sha256") == hashed(row.get("evidence")), "fingerprint_evidence_changed")
            require(row["origin"]["provider"] == provider and row["origin"]["scope"] == expected_scope, "fingerprint_scope_changed")
            closed = checks.get("verified_cleanup", {})
            require(bundle.get("cleanup") == "verified" and closed.get("status") == "pass"
                    and closed.get("origin") == row["origin"]
                    and closed.get("proof_sha256") == hashed({k: v for k, v in closed.items() if k != "proof_sha256"})
                    and closed.get("evidence_sha256") == hashed(closed.get("evidence")), "fingerprint_original_cleanup_unverified")
            receipt = closed["evidence"]
            try:
                # Native query receipts contain positive counts, not booleans.
                # Reuse the accounting validator, including the original scope.
                validate_cleanup_receipt(receipt, {"provider": provider, "started_at": 0}, expected_scope)
            except (PolicyError, ValueError, KeyError, TypeError):
                raise PolicyError("fingerprint_original_cleanup_unverified") from None
            require(row["input_fingerprint"] == input_fingerprint(name, components), "fingerprint_inputs_changed")
            require(row["verifier_fingerprint"] == verifier(root, name), "fingerprint_verifier_changed")
            require(set(row["dependencies"]) == set(SPECS[name][1]), "fingerprint_dependencies_changed")
            for dependency, expected in row["dependencies"].items():
                require(visit(dependency)["status"] == "carry", "fingerprint_dependency_needs_reproof")
                old = checks[dependency]
                require(expected == hashed({"input": old["input_fingerprint"], "verifier": old["verifier_fingerprint"]}), "fingerprint_dependencies_changed")
            status = {"status": "carry", "origin": row["origin"], "proof_sha256": row["proof_sha256"]}
        except (PolicyError, ValueError, KeyError, TypeError) as error:
            reason = str(error) if str(error).startswith(("fingerprint_", "fresh_cleanup_", "missing_fingerprint")) else "missing_fingerprints"
            status = {"status": "reproof", "reason": reason}
        visiting.discard(name); result[name] = status
        return status
    names = ("vm_and_containers",) if bundle.get("mode") == "light" else tuple(k for k in GROUPS if k != "vm_and_containers")
    groups = {}
    for name in names:
        states = {key: visit(key) for key in GROUPS[name]}
        groups[name] = {"status": "carry" if all(row["status"] == "carry" for row in states.values()) else "reproof", "checks": states}
    return {"schema": SCHEMA, "provider": provider, "groups": groups, "checks": result}


def main():
    parser = argparse.ArgumentParser(description="Review prior acceptance against a candidate normalized plan and observed contents; never launches or spends.")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    for name in ("config", "plan", "run", "contents", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--provider", choices=("aws", "digitalocean", "gcp", "hetzner"), required=True)
    parser.add_argument("--documentation", type=Path, help="Verified linked plan IDs and their content checksums, for documentation review")
    args = parser.parse_args()
    config = private_read(args.config); record = private_read(args.run); contents = private_read(args.contents)
    recipe = recipe_from_plan(private_read(args.plan), args.provider)
    bundle = record.get("fingerprints", {"schema": SCHEMA, "mode": record.get("acceptance_mode"), "checks": {}})
    components = context(args.repo, config, args.provider, recipe, contents, private_read(args.documentation) if args.documentation else None)
    report = evaluate(args.repo, bundle, components, args.provider, scope(config, args.provider))
    private_write(args.output, report)
    print(json.dumps({"provider": args.provider, "groups": {key: value["status"] for key, value in report["groups"].items()}}))


if __name__ == "__main__":
    main()
