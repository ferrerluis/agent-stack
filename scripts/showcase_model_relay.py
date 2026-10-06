#!/usr/bin/env python3
"""Checked VM-side private bridge relay; stdin controls its bounded lifetime.

No files, credentials, public binds, arbitrary upstreams or daemon services
are created. Controller source/ownership and pinned native routes still need
independent acceptance. This is not a cloud launch command.
"""
from http.client import HTTPConnection
import ipaddress
import json
import math
import os
import pwd
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import threading
import time

MAX_CONNECTIONS = 4
MAX_BYTES = 8 * 1024 * 1024
MAX_BUFFER = 65536
SESSION_SECONDS = 120


def require(value):
    if not value: raise ValueError("model_relay_refused")


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value); value[key] = item
    return value


def decoded(raw):
    require(isinstance(raw, bytes) and len(raw) <= 8192)
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))
    require(isinstance(value, dict)); return value


def server_policy():
    connection = os.environ["SSH_CONNECTION"].split()
    require(len(connection) == 4)
    for index in (0, 2): ipaddress.ip_address(connection[index])
    for index in (1, 3): require(re.fullmatch(r"[1-9][0-9]{0,4}", connection[index]) and int(connection[index]) <= 65535)
    user = pwd.getpwuid(os.geteuid()).pw_name
    require(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user))
    match = "user=" + user + ",host=" + connection[0] + ",addr=" + connection[0] + ",laddr=" + connection[2] + ",lport=" + connection[3]
    result = subprocess.run(["sudo", "-n", "/usr/sbin/sshd", "-T", "-C", match], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=5, check=True)
    require(len(result.stdout) <= 65536)
    values = {}
    for line in result.stdout.decode().splitlines():
        key, separator, value = line.partition(" ")
        require(separator)
        if key in ("gatewayports", "disableforwarding", "allowtcpforwarding", "pubkeyauthentication", "permitlisten", "usedns"):
            require(key not in values); values[key] = value
    require(values.get("gatewayports") == "no" and values.get("disableforwarding") == "no"
            and values.get("allowtcpforwarding") in ("yes", "all", "remote")
            and values.get("pubkeyauthentication") == "yes" and values.get("permitlisten") in ("any", "127.0.0.1:*")
            and values.get("usedns") == "no")
    return {"schema": 1, "status": "forwarding_policy_verified", "gateway_ports": "no",
            "allow_tcp_forwarding": values["allowtcpforwarding"], "disable_forwarding": "no"}


def bridge_address():
    result = subprocess.run(["/usr/bin/docker", "network", "inspect", "bridge"], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=5, check=True)
    require(len(result.stdout) <= 65536)
    rows = json.loads(result.stdout, object_pairs_hook=unique)
    require(isinstance(rows, list) and len(rows) == 1)
    row = rows[0]
    require(row.get("Name") == "bridge" and row.get("Driver") == "bridge" and row.get("Internal") is False)
    configs = row.get("IPAM", {}).get("Config", [])
    ipv4 = []
    for item in configs:
        address = ipaddress.ip_address(item.get("Gateway", ""))
        subnet = ipaddress.ip_network(item.get("Subnet", ""), strict=True)
        require(address.version == subnet.version and address in subnet)
        if address.version == 4:
            require(any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
            require(address not in (subnet.network_address, subnet.broadcast_address)); ipv4.append(str(address))
    require(len(ipv4) == 1); return ipv4[0]


def owned_binding(packet, now=time.time):
    require(set(packet) == {"run_id", "boot_id", "checksum", "forward_port", "sent_at", "work_deadline"})
    require(isinstance(packet["run_id"], str) and re.fullmatch(r"as7-[a-z0-9-]{8,30}", packet["run_id"]))
    require(isinstance(packet["boot_id"], str) and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", packet["boot_id"]))
    require(isinstance(packet["checksum"], str) and re.fullmatch(r"[a-f0-9]{64}", packet["checksum"]))
    require(type(packet["forward_port"]) is int and 1 <= packet["forward_port"] <= 65535)
    current = now()
    require(all(type(packet[name]) in (int, float) and math.isfinite(packet[name]) for name in ("sent_at", "work_deadline")))
    require(abs(current - packet["sent_at"]) <= 5 and 10 < packet["work_deadline"] - current <= 14400)
    require(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == packet["boot_id"])
    applied = decoded(Path("/opt/agent-stack/.last-apply.json").read_bytes())
    require(applied.get("status") == "success" and applied.get("checksum") == packet["checksum"]
            and sorted(applied.get("services", [])) == ["hermes", "openclaw", "workspace"])
    result = subprocess.run(["/usr/bin/ss", "-H", "-ltn"], stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=True)
    require(len(result.stdout) <= 65536)
    suffix = ":" + str(packet["forward_port"])
    listeners = [fields[3] for line in result.stdout.decode().splitlines() if len(fields := line.split()) >= 5 and fields[3].endswith(suffix)]
    require(listeners and all(address == "127.0.0.1" + suffix for address in listeners))
    return min(packet["work_deadline"] - current - 5, 14395)


class PrivateRelay:
    def __init__(self, bridge, forward_port, lifetime, now=time.monotonic):
        require(type(forward_port) is int and 1 <= forward_port <= 65535 and type(lifetime) in (int, float)
                and math.isfinite(lifetime) and 0 < lifetime <= 14395)
        self.bridge, self.forward_port, self.now = bridge, forward_port, now
        self.deadline = now() + lifetime
        self.condition = threading.Condition(); self.connections = {}; self.workers = set()
        self.stop = threading.Event(); self.server = socket.socket()
        try:
            self.server.bind((bridge, 0)); self.server.listen(MAX_CONNECTIONS); self.server.settimeout(.1)
        except BaseException:
            self.server.close(); raise
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.accept, daemon=True); self.thread.start()

    def healthy(self): return not self.stop.is_set() and self.now() < self.deadline and self.thread.is_alive()

    def accept(self):
        while not self.stop.is_set() and self.now() < self.deadline:
            try: client, _ = self.server.accept()
            except socket.timeout: continue
            except OSError: break
            with self.condition:
                if len(self.connections) >= MAX_CONNECTIONS:
                    client.close(); continue
                worker = threading.Thread(target=self.forward, args=(client,), daemon=True)
                self.connections[client] = None; self.workers.add(worker); worker.start()
        self.stop.set()
        with self.condition:
            sockets = [sock for pair in self.connections.items() for sock in pair if sock is not None]
        for sock in sockets: self.shutdown(sock)

    @staticmethod
    def shutdown(sock):
        try: sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def forward(self, client):
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", self.forward_port), timeout=2)
            with self.condition: self.connections[client] = upstream
            endpoints = (client, upstream)
            for sock in endpoints: sock.setblocking(False)
            buffers, counts, eof = {s: bytearray() for s in endpoints}, {s: 0 for s in endpoints}, set()
            deadline = min(self.deadline, self.now() + SESSION_SECONDS)
            while self.healthy() and self.now() < deadline:
                readers = [s for s in endpoints if s not in eof and len(buffers[endpoints[1 - endpoints.index(s)]]) < MAX_BUFFER]
                writers = [s for s in endpoints if buffers[s]]
                if not readers and not writers: break
                readable, writable, _ = select.select(readers, writers, [], .05)
                for sock in writable:
                    try: sent = sock.send(buffers[sock])
                    except BlockingIOError: continue
                    require(sent > 0); del buffers[sock][:sent]
                    other = endpoints[1 - endpoints.index(sock)]
                    if not buffers[sock] and other in eof:
                        try: sock.shutdown(socket.SHUT_WR)
                        except OSError: pass
                for sock in readable:
                    other = endpoints[1 - endpoints.index(sock)]
                    try: raw = sock.recv(min(8192, MAX_BUFFER - len(buffers[other])))
                    except BlockingIOError: continue
                    if not raw:
                        eof.add(sock)
                        if not buffers[other]:
                            try: other.shutdown(socket.SHUT_WR)
                            except OSError: pass
                    else:
                        counts[sock] += len(raw); require(counts[sock] <= MAX_BYTES); buffers[other].extend(raw)
        except Exception: pass
        finally:
            for sock in (client, upstream):
                if sock is not None: self.shutdown(sock); sock.close()
            with self.condition:
                self.connections.pop(client, None); self.workers.discard(threading.current_thread()); self.condition.notify_all()

    def close(self):
        self.stop.set(); self.server.close()
        with self.condition:
            sockets = [sock for pair in self.connections.items() for sock in pair if sock is not None]
        for sock in sockets: self.shutdown(sock)
        self.thread.join(timeout=1)
        deadline = time.monotonic() + 3
        with self.condition:
            while self.workers and time.monotonic() < deadline: self.condition.wait(.05)
            require(not self.workers and not self.connections and not self.thread.is_alive())


class ControlReader:
    def __init__(self, descriptor):
        self.fd, self.buffer, self.started = descriptor, bytearray(), None
        os.set_blocking(descriptor, False)

    def packet(self, timeout=.2):
        deadline = time.monotonic() + timeout
        while True:
            require(len(self.buffer) <= 8192 and (self.started is None or time.monotonic() - self.started < 5))
            if b"\n" in self.buffer:
                raw, _, tail = self.buffer.partition(b"\n"); self.buffer = bytearray(tail)
                self.started = time.monotonic() if self.buffer else None
                return decoded(bytes(raw))
            remaining = deadline - time.monotonic()
            if remaining <= 0: return None
            readable, _, _ = select.select([self.fd], [], [], min(.1, remaining))
            if not readable: continue
            raw = os.read(self.fd, 8193)
            if not raw: raise EOFError
            if self.started is None: self.started = time.monotonic()
            self.buffer.extend(raw)


def probe(relay, packet):
    require(set(packet) == {"role", "nonce", "credential"} and packet["role"] == "codex"
            and all(isinstance(packet[name], str) and re.fullmatch(r"[a-f0-9]{64}", packet[name]) for name in ("nonce", "credential")))
    connection = HTTPConnection(relay.bridge, relay.port, timeout=5)
    try:
        connection.request("POST", "/codex/_transport", json.dumps({"nonce": packet["nonce"]}, separators=(",", ":")),
                           {"Content-Type": "application/json", "Authorization": "Bearer " + packet["credential"]})
        response = connection.getresponse(); raw = response.read(1025)
        require(response.status == 200 and len(raw) <= 1024)
        result = decoded(raw)
        require(set(result) == {"schema", "role", "nonce", "source_revision"} and type(result["schema"]) is int and result["schema"] == 1
                and result["role"] == "codex" and result["nonce"] == packet["nonce"]
                and isinstance(result["source_revision"], str) and re.fullmatch(r"[a-f0-9]{40}", result["source_revision"]))
        return result
    finally: connection.close(); packet.clear()


def emit(value): print(json.dumps(value, separators=(",", ":")), flush=True)


def main():
    relay = None; reason = "controller_eof"; code = 0
    try:
        reader = ControlReader(sys.stdin.fileno()); initial = reader.packet(15)
        require(initial is not None)
        started = time.monotonic(); lifetime = owned_binding(initial); bridge = bridge_address()
        # Metadata/discovery time belongs to the original lease too.
        lifetime -= time.monotonic() - started
        relay = PrivateRelay(bridge, initial["forward_port"], lifetime)
        emit({"schema": 1, "status": "ready", "bridge_ipv4": relay.bridge, "bridge_port": relay.port,
              **{name: initial[name] for name in ("boot_id", "checksum", "forward_port")}})
        proved = False
        while relay.healthy():
            packet = reader.packet()
            if packet is None: continue
            if packet == {"stop": True}: reason = "controller_stop"; break
            require(not proved); reply = probe(relay, packet); proved = True
            emit({"schema": 1, "status": "probe", "reply": reply})
        else: reason = "deadline_or_listener_lost"; code = 1
    except EOFError: pass
    except BaseException: reason = "model_relay_failed"; code = 1
    finally:
        try:
            if relay is not None: relay.close()
            emit({"schema": 1, "status": "closed", "reason": reason, "listeners_closed": True, "connections_closed": True})
        except BaseException: emit({"schema": 1, "status": "unverified"}); code = 1
    return code


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-forwarding"]:
        try: emit(server_policy())
        except BaseException: emit({"schema": 1, "status": "forwarding_policy_refused"}); sys.exit(1)
    elif sys.argv[1:]: emit({"schema": 1, "status": "model_relay_refused"}); sys.exit(1)
    else: sys.exit(main())
