"""A finite, task-owned SSH master and a client that cannot reconnect silently.

This module grants no connection authority. Its caller supplies reviewed immutable
bindings, owns the master invocation, and retains the resulting receipts. The
client replaces itself with ssh so a surrounding bounded collector still owns
the actual transport process and its standard streams.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time


SCHEMA = "shared.owned_ssh_control.v1"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
STREAM_LIMIT = 65_536


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "regular file required")
        return stream.read()


def pin(path):
    path = Path(path)
    data = read_regular(path)
    return {"path": str(path.absolute()), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def checked(ref):
    require(type(ref) is dict and set(ref) == {"path", "bytes", "sha256"}, "invalid pin")
    require(type(ref["bytes"]) is int and ref["bytes"] >= 0, "invalid pin size")
    require(Path(ref["path"]).is_absolute(), "absolute pin required")
    data = read_regular(ref["path"])
    require(len(data) == ref["bytes"] and hashlib.sha256(data).hexdigest() == ref["sha256"],
            "pin mismatch")
    return data


def document(ref):
    require(ref["bytes"] <= 65_536, "control document too large")
    return json.loads(checked(ref))


def save(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    require(len(data) <= 32_768, "receipt too large")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return pin(path)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def process(pid):
    base = Path("/proc") / str(pid)
    fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
    require(fields[0] != "Z", "master is a zombie")
    return {"pid": pid, "start_ticks": fields[19],
            "argv": (base / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")}


def private_directory(path):
    path = Path(path)
    value = path.lstat()
    require(stat.S_ISDIR(value.st_mode) and not path.is_symlink(), "private directory required")
    require(value.st_uid == os.getuid() and stat.S_IMODE(value.st_mode) == 0o700,
            "directory owner or mode changed")
    require(str(path.resolve()) == str(path), "directory path must be canonical")


def socket_identity(path):
    value = Path(path).lstat()
    require(stat.S_ISSOCK(value.st_mode), "control path is not a socket")
    require(value.st_uid == os.getuid() and value.st_mode & 0o077 == 0,
            "control socket owner or mode changed")
    return {"device": value.st_dev, "inode": value.st_ino, "uid": value.st_uid,
            "mode": stat.S_IMODE(value.st_mode)}


def socket_fds(pid):
    result = []
    for entry in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            result.append(target)
    return sorted(set(result))


def binding(ref):
    cfg = document(ref)
    keys = {"schema", "source", "ssh", "false_executable", "destination", "connection",
            "remote_command", "control_directory", "output_directory", "maximum_lifetime_seconds",
            "startup_timeout_seconds"}
    require(set(cfg) == keys and cfg["schema"] == SCHEMA, "unknown binding schema or fields")
    require(cfg["source"] == pin(__file__), "wrong transport source")
    for name in ("source", "ssh", "false_executable"):
        checked(cfg[name])
    require(os.access(cfg["ssh"]["path"], os.X_OK) and
            os.access(cfg["false_executable"]["path"], os.X_OK), "executable required")
    require(cfg["false_executable"]["path"] == "/usr/bin/false", "unqualified fallback executable")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", cfg["destination"]) is not None,
            "invalid destination")
    connection = cfg["connection"]
    require(set(connection) == {"hostname", "port", "user", "identity_file", "known_hosts_file",
                                "connect_timeout", "server_alive_interval", "server_alive_count_max"},
            "unknown connection fields")
    for key in ("hostname", "user"):
        require(type(connection[key]) is str and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", connection[key]) is not None,
                "invalid connection identity")
    for key, upper in (("port", 65_535), ("connect_timeout", 60),
                       ("server_alive_interval", 300), ("server_alive_count_max", 10)):
        require(type(connection[key]) is int and 0 < connection[key] <= upper,
                "invalid finite connection limit")
    for key in ("identity_file", "known_hosts_file"):
        require(type(connection[key]) is str and Path(connection[key]).is_absolute() and
                "\n" not in connection[key] and "\0" not in connection[key], "invalid auth path")
    for key in ("control_directory", "output_directory"):
        require(type(cfg[key]) is str and Path(cfg[key]).is_absolute() and
                str(Path(cfg[key])) == cfg[key], "absolute canonical directory required")
    require(cfg["control_directory"] != cfg["output_directory"], "separate output directory required")
    require(len(os.fsencode(cfg["control_directory"] + "/control")) <= 90, "control path too long")
    require(type(cfg["remote_command"]) is str and 0 < len(cfg["remote_command"].encode()) <= 131_072
            and "\0" not in cfg["remote_command"], "invalid exact remote command")
    for key, upper in (("maximum_lifetime_seconds", 86_400), ("startup_timeout_seconds", 60)):
        require(type(cfg[key]) is int and 0 < cfg[key] <= upper, "invalid finite lifetime")
    return cfg


def argv(cfg, master):
    connection = cfg["connection"]
    result = [cfg["ssh"]["path"], "-F", "/dev/null", "-T", "-S",
              cfg["control_directory"] + "/control"]
    options = {
        "ControlMaster": "yes" if master else "no", "ControlPersist": "no",
        "ProxyCommand": "none" if master else cfg["false_executable"]["path"],
        "BatchMode": "yes", "StrictHostKeyChecking": "yes", "IdentitiesOnly": "yes",
        "IdentityAgent": "none", "ForwardAgent": "no", "ClearAllForwardings": "yes",
        "PermitLocalCommand": "no", "RequestTTY": "no", "ExitOnForwardFailure": "yes",
        "Hostname": connection["hostname"], "Port": str(connection["port"]),
        "User": connection["user"], "IdentityFile": connection["identity_file"],
        "UserKnownHostsFile": connection["known_hosts_file"], "GlobalKnownHostsFile": "/dev/null",
        "ConnectTimeout": str(connection["connect_timeout"]),
        "ServerAliveInterval": str(connection["server_alive_interval"]),
        "ServerAliveCountMax": str(connection["server_alive_count_max"]),
    }
    for key, value in options.items():
        result.extend(["-o", key + "=" + value])
    if master:
        result.append("-N")
    result.append(cfg["destination"])
    if not master:
        result.append(cfg["remote_command"])
    return result


def ready(ref, ready_ref):
    cfg = binding(ref)
    record = document(ready_ref)
    require(record["schema"] == SCHEMA + ".ready" and record["binding"] == ref, "wrong READY")
    require(record["source"] == cfg["source"] and record["master"]["argv"] == argv(cfg, True),
            "wrong master source or argv")
    require(record["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "master boot changed")
    require(time.monotonic() < record["expires_monotonic"], "master lifetime expired")
    private_directory(cfg["control_directory"])
    private_directory(cfg["output_directory"])
    require(not (Path(cfg["output_directory"]) / "STOP.json").exists(), "master stopping")
    require(not (Path(cfg["output_directory"]) / "RESULT.json").exists(), "master ended")
    require(process(record["master"]["pid"]) == record["master"], "master process identity changed")
    require(socket_identity(cfg["control_directory"] + "/control") == record["socket"],
            "control socket changed")
    require(record["master_socket_fds"] and
            set(record["master_socket_fds"]) <= set(socket_fds(record["master"]["pid"])),
            "master socket ownership changed")
    require(record["control_check"] == "Master running (pid=%d)\r\n" % record["master"]["pid"],
            "master control check missing")
    return cfg, record


def exec_client(ref, ready_ref, destination, command):
    cfg, _ = ready(ref, ready_ref)
    require(destination == cfg["destination"] and command == cfg["remote_command"],
            "destination or exact remote command changed")
    # No stdin read, output redirection, retry, fork, or fresh-connection fallback.
    os.execve(cfg["ssh"]["path"], argv(cfg, False), ENV)


def stop(ref, ready_ref):
    cfg, _ = ready(ref, ready_ref)
    return save(Path(cfg["output_directory"]) / "STOP.json",
                {"binding": ref, "ready": ready_ref, "at": now(), "stop": True})


def parent_death(parent_pid):
    # Linux PR_SET_PDEATHSIG closes this owned master if its supervisor is killed.
    require(ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) == 0,
            "cannot bind master lifetime to supervisor")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def interrupted(signum, frame):
    # selectors may consume InterruptedError as an interrupted system call.
    raise RuntimeError("supervisor signal %d" % signum)


def run_master(ref):
    cfg = binding(ref)
    output = Path(cfg["output_directory"])
    control = Path(cfg["control_directory"])
    output.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    private_directory(output)
    private_directory(control)
    lock = (output / "LOCK").open("xb")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    deadline = started + cfg["maximum_lifetime_seconds"]
    command = argv(cfg, True)
    save(output / "INTENT.json", {"at": now(), "binding": ref, "argv": command,
                                  "automatic_retry": False})
    child = None
    selector = None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    ready_ref = None
    reason = "master_exit"
    failure = None
    old_handlers = {}
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            old_handlers[number] = signal.signal(number, interrupted)
        parent_pid = os.getpid()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=ENV, start_new_session=True,
                                 preexec_fn=lambda: parent_death(parent_pid))
        selector = selectors.DefaultSelector()
        for name in streams:
            pipe = getattr(child, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while True:
            for key, _ in selector.select(0.05):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                require(len(streams[name]) + len(data) <= STREAM_LIMIT, "master stream limit")
                streams[name].extend(data)
            if child.poll() is not None:
                break
            elapsed = time.monotonic()
            if elapsed >= deadline:
                reason = "lifetime_expired"
                break
            if ready_ref is None and (control / "control").exists():
                identity = process(child.pid)
                require(identity["argv"] == command, "master argv changed")
                check = subprocess.run([cfg["ssh"]["path"], "-F", "/dev/null", "-S",
                                        str(control / "control"), "-O", "check", cfg["destination"]],
                                       stdin=subprocess.DEVNULL, capture_output=True, timeout=3, env=ENV)
                expected = "Master running (pid=%d)\r\n" % child.pid
                require(check.returncode == 0 and check.stdout == b"" and
                        check.stderr.decode() == expected, "local master control check failed")
                record = {"schema": SCHEMA + ".ready", "at": now(), "binding": ref,
                          "source": cfg["source"], "master": identity,
                          "socket": socket_identity(control / "control"),
                          "master_socket_fds": socket_fds(child.pid),
                          "control_check": expected, "expires_monotonic": deadline,
                          "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
                require(record["master_socket_fds"], "master sockets absent")
                ready_ref = save(output / "READY.json", record)
            if ready_ref is None:
                require(elapsed - started <= cfg["startup_timeout_seconds"], "master startup deadline")
            stop_path = output / "STOP.json"
            if stop_path.exists():
                value = document(pin(stop_path))
                require(value["binding"] == ref and value["ready"] == ready_ref and
                        value["stop"] is True, "invalid owned stop request")
                reason = "requested_stop"
                break
    except BaseException as error:
        failure = {"type": type(error).__name__, "message": str(error)[:2048]}
        reason = "supervisor_error"
    finally:
        for number in old_handlers:
            signal.signal(number, signal.SIG_IGN)
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=3)
        for name in ("stdout", "stderr"):
            pipe = None if child is None else getattr(child, name)
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
            remaining = b"" if pipe is None else (pipe.read(STREAM_LIMIT + 1) or b"")
            if len(streams[name]) + len(remaining) > STREAM_LIMIT:
                failure = failure or {"type": "ValueError", "message": "master stream limit"}
                reason = "supervisor_error"
            streams[name].extend(remaining[:max(0, STREAM_LIMIT - len(streams[name]))])
            with (output / (name + ".bin")).open("xb") as sink:
                sink.write(streams[name])
            if pipe is not None:
                pipe.close()
        if selector is not None:
            selector.close()
        result = {"at": now(), "binding": ref, "ready": ready_ref, "reason": reason,
                  "returncode": None if child is None else child.returncode,
                  "error": failure, "automatic_retry": False,
                  "stdout": pin(output / "stdout.bin"), "stderr": pin(output / "stderr.bin")}
        save(output / "RESULT.json", result)
        lock.close()
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("master", "client", "stop"))
    parser.add_argument("--binding", required=True, type=json.loads)
    parser.add_argument("--ready", type=json.loads)
    parser.add_argument("destination", nargs="?")
    parser.add_argument("remote_command", nargs="?")
    args = parser.parse_intermixed_args()
    if args.mode == "client":
        exec_client(args.binding, args.ready, args.destination, args.remote_command)
    elif args.mode == "stop":
        print(json.dumps(stop(args.binding, args.ready)))
    else:
        result = run_master(args.binding)
        print(json.dumps(result))
        return 0 if result["reason"] in ("requested_stop", "lifetime_expired") and not result["error"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
