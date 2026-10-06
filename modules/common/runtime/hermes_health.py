#!/usr/bin/env python3
"""Authenticated, bounded Hermes readiness check; never emit credentials/errors."""
import json
import os
import sys
import urllib.request

REQUIRED_CHECKS = {"state_db", "session_store", "config", "model", "disk", "gateway", "background_queues"}


def healthy(payload):
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    readiness = payload.get("readiness")
    if not isinstance(readiness, dict):
        return False
    checks = readiness.get("checks")
    if not isinstance(checks, dict) or not REQUIRED_CHECKS <= checks.keys():
        return False
    return all(isinstance(check, dict) and check.get("status") == "ok" for check in checks.values())


def main():
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        print(json.dumps({"status": "fail", "reason": "missing_api_key"}))
        return 1
    try:
        req = urllib.request.Request("http://127.0.0.1:8642/health/detailed", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=5) as response:
            passed = response.status == 200 and healthy(json.load(response))
    except Exception:
        passed = False
    print(json.dumps({"status": "pass" if passed else "fail", "reason": "ready" if passed else "readiness_failed"}))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
