"""Run an explicitly authorized finite process group inside an existing tmux pane."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pin(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("noncanonical_path")
    raw = path.read_bytes()
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def read(ref):
    if pin(ref["path"]) != ref:
        raise ValueError("pin_mismatch")
    return json.loads(Path(ref["path"]).read_bytes())


def save(path, value):
    path = Path(path)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return pin(path)


def identity(pid):
    proc = Path("/proc") / str(pid)
    stat = (proc / "stat").read_text()
    tail = stat[stat.rfind(")") + 2:].split()
    return {"pid": pid, "start_ticks": tail[19], "state": tail[0],
            "argv": (proc / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")}


def observe_process(child, expected_argv, intent):
    """Keep dated observations and select only the same child's identity.

    Observation failures do not change the child's recorded execution outcome.
    """
    started = time.monotonic()
    deadline = started + 0.5
    observations = []
    initial = selected = None
    returncode = None
    while True:
        try:
            process = identity(child.pid)
        except OSError as error:
            observations.append({"at": now(), "error": repr(error)})
            returncode = child.poll()
            status = "exited" if returncode is not None else "unavailable"
            break
        observation = {"at": now(), "identity": process}
        observations.append(observation)
        if initial is None:
            initial = process
        if process["pid"] != child.pid or process["start_ticks"] != initial["start_ticks"]:
            status = "identity_changed"
            break
        selected = observation
        returncode = child.poll()
        if returncode is not None:
            status = "exited"
            break
        if process["argv"] == expected_argv:
            status = "matched"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status = "timeout"
            break
        time.sleep(min(0.01, remaining))
        # Re-observe only while the same child is still live.
        returncode = child.poll()
        if returncode is not None:
            status = "exited"
            break
    if selected is None:
        selected = {"at": observations[0]["at"], "identity": {
            "pid": child.pid, "already_exited": returncode is not None}}
    argv = selected["identity"].get("argv")
    argv_status = ("matched" if argv == expected_argv else
                   "unavailable" if argv is None else
                   "empty" if argv in ([], [""]) else "mismatch")
    return {**selected, "intent": intent, "identity_status": status,
            "argv_status": argv_status, "observations": observations,
            "expected_argv": expected_argv, "returncode_at_observation": returncode,
            "observation_timeout_seconds": 0.5,
            "observation_elapsed_seconds": time.monotonic() - started}


def validate(packet_ref, adoption_ref):
    packet, adoption = read(packet_ref), read(adoption_ref)
    if adoption.get("root_authorized") is not True or adoption.get("packet") != packet_ref:
        raise ValueError("authorization_mismatch")
    if adoption.get("automatic_retry") is not False or adoption.get("maximum_invocations_per_actor") != 1:
        raise ValueError("retry_not_authorized")
    if adoption.get("attempt_id") != packet["attempt_id"]:
        raise ValueError("attempt_mismatch")
    if pin(__file__) != packet["supervisor_source"]:
        raise ValueError("supervisor_source_changed")
    names = [actor["name"] for actor in packet["actors"]]
    if not names or len(set(names)) != len(names):
        raise ValueError("duplicate_or_empty_actors")
    output = Path(packet["output_directory"])
    if not output.is_absolute() or output.resolve() != output or output.exists():
        raise ValueError("output_not_exclusive")
    for actor in packet["actors"]:
        if not actor["name"].replace("_", "").isalnum():
            raise ValueError("invalid_actor_name")
        argv = actor["argv"]
        if not argv or not all(type(arg) is str for arg in argv):
            raise ValueError("invalid_argv")
        if actor["source"]["path"] not in argv:
            raise ValueError("source_not_in_argv")
        for ref in actor["pins"]:
            if pin(ref["path"]) != ref:
                raise ValueError("actor_dependency_changed")
        if actor["source"] not in actor["pins"]:
            raise ValueError("source_not_pinned")
    for name in packet.get("must_be_absent", []):
        if Path(name).exists():
            raise ValueError("prelaunch_path_present: " + name)
    for name in packet.get("must_be_empty", []):
        if not Path(name).is_dir() or any(Path(name).iterdir()):
            raise ValueError("prelaunch_directory_not_empty: " + name)
    return packet, adoption


def run(packet_ref, adoption_ref):
    packet, adoption = validate(packet_ref, adoption_ref)
    if not os.environ.get("TMUX"):
        raise ValueError("tmux_required")
    pane = subprocess.run(["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{session_name}:#{window_id}:#{pane_id}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    output = Path(packet["output_directory"])
    output.mkdir()
    save(output / "INTENT.json", {"at": now(), "packet": packet_ref, "adoption": adoption_ref,
                                 "attempt_id": packet["attempt_id"], "tmux_pane": pane,
                                 "supervisor": identity(os.getpid()), "automatic_retry": False})
    live = []
    launch_error = None
    try:
        for actor in packet["actors"]:
            actor_dir = output / actor["name"]
            actor_dir.mkdir()
            intent = save(actor_dir / "INTENT.json", {"at": now(), "actor": actor, "packet": packet_ref,
                          "adoption": adoption_ref, "attempt_id": packet["attempt_id"], "automatic_retry": False})
            stdout = (actor_dir / "stdout").open("xb")
            stderr = (actor_dir / "stderr").open("xb")
            try:
                child = subprocess.Popen(actor["argv"], cwd=packet["cwd"], stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr)
            finally:
                stdout.close()
                stderr.close()
            row = {"actor": actor, "directory": actor_dir, "child": child, "intent": intent,
                   "started_at": now(), "clock": time.monotonic()}
            live.append(row)
            save(actor_dir / "PROCESS.json", observe_process(child, actor["argv"], intent))
        save(output / "LAUNCHED.json", {"at": now(), "actors": [row["actor"]["name"] for row in live],
                                        "automatic_retry": False, "maximum_invocations_per_actor": 1})
    except BaseException as error:
        launch_error = repr(error)
        save(output / "LAUNCH_ERROR.json", {"at": now(), "error": repr(error),
             "started_actors": [row["actor"]["name"] for row in live],
             "signals_sent": False, "automatic_retry": False})
    results = []
    actor_failed = False
    while live:
        for row in list(live):
            returncode = row["child"].poll()
            if returncode is None:
                continue
            source_error = None
            try:
                source_stable = pin(row["actor"]["source"]["path"]) == row["actor"]["source"]
            except Exception as error:
                source_stable = False
                source_error = repr(error)
            result = {"at": now(), "started_at": row["started_at"], "elapsed_seconds": time.monotonic() - row["clock"],
                      "returncode": returncode, "actor": row["actor"]["name"], "intent": row["intent"],
                      "source_stable": source_stable, "source_error": source_error,
                      "stdout": pin(row["directory"] / "stdout"), "stderr": pin(row["directory"] / "stderr"),
                      "automatic_retry": False, "signals_sent": False}
            results.append(save(row["directory"] / "EXECUTION.json", result))
            actor_failed = actor_failed or returncode != 0 or not source_stable
            live.remove(row)
        if live:
            time.sleep(0.1)
    save(output / "COMPLETE.json", {"at": now(), "executions": results, "launch_error": launch_error,
                                    "all_actors_succeeded": launch_error is None and not actor_failed,
                                    "automatic_retry": False})
    return 1 if launch_error is not None or actor_failed else 0


if __name__ == "__main__":
    raise SystemExit(run(*(json.loads(arg) for arg in sys.argv[1:])))
