#!/usr/bin/env python3
"""Private nonbillable route check inside the owned workspace, as UID 1000.

The checked controller sends this source and a bounded private bundle via SSH.
It offers no endpoint override and never receives an upstream provider key.
"""
import hashlib
from http.client import HTTPConnection
import ipaddress
import json
import os
import re
import socket
import sys


def need(value):
    if not value: raise ValueError("workspace_model_route_unverified")


def decoded(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result); result[key] = value
        return result
    def constant(_): raise ValueError("workspace_model_route_unverified")
    need(isinstance(raw, bytes) and 0 < len(raw) <= 4096)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    need(isinstance(value, dict))
    return value


def probe(packet):
    need(os.geteuid() == 1000 and isinstance(packet, dict) and set(packet) ==
         {"role", "provider", "base_url", "credential", "nonce", "bridge_ipv4", "source_revision"}
         and all(isinstance(value, str) for value in packet.values()))
    role = packet["role"]
    need(role in ("codex", "claude") and packet["provider"] == ("openai" if role == "codex" else "anthropic")
         and re.fullmatch(r"[a-f0-9]{64}", packet["credential"]) and re.fullmatch(r"[a-f0-9]{64}", packet["nonce"])
         and re.fullmatch(r"[a-f0-9]{40}", packet["source_revision"]))
    route = re.fullmatch(r"http://host\.docker\.internal:([1-9][0-9]{0,4})/" + role, packet["base_url"])
    need(route and int(route[1]) <= 65535)
    address = ipaddress.ip_address(packet["bridge_ipv4"])
    need(address.version == 4 and any(address in ipaddress.ip_network(net) for net in
         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
    resolved = socket.getaddrinfo("host.docker.internal", int(route[1]), type=socket.SOCK_STREAM)
    need(resolved and all(row[0] == socket.AF_INET and row[4][0] == str(address) for row in resolved))
    # Connect to the independently checked address rather than resolving a
    # second time after the check. No proxy, redirect, TLS bypass or retry.
    connection = HTTPConnection(str(address), int(route[1]), timeout=3)
    try:
        body = json.dumps({"nonce": packet["nonce"]}, separators=(",", ":")).encode()
        connection.request("POST", "/" + role + "/_transport", body,
                           {"Authorization": "Bearer " + packet["credential"], "Content-Type": "application/json"})
        response = connection.getresponse()
        need(response.status == 200 and response.getheader("Content-Type") == "application/json")
        raw = response.read(1025); need(len(raw) <= 1024); reply = decoded(raw)
        need(reply == {"schema": 1, "role": role, "nonce": packet["nonce"], "source_revision": packet["source_revision"]}
             and type(reply.get("schema")) is int)
    finally: connection.close()
    return {"schema": 1, "status": "workspace_route_verified", "role": role, "provider": packet["provider"],
            "uid": 1000, "bridge_ipv4": str(address), "nonce": packet["nonce"], "source_revision": packet["source_revision"],
            "base_url_sha256": hashlib.sha256(packet["base_url"].encode()).hexdigest()}


def main():
    try: result = probe(decoded(sys.stdin.buffer.read(4097)))
    except Exception: result = {"schema": 1, "status": "workspace_route_unverified"}
    print(json.dumps(result))
    return 0 if result["status"] == "workspace_route_verified" else 1


if __name__ == "__main__": sys.exit(main())
