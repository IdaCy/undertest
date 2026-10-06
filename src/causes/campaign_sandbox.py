"""Isolated repository workspaces with retained filesystem and process evidence."""

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import time
import uuid


_MASKS = ("/testbed", "/root", "/home", "/swebench_matterhorn")
_APPARMOR_PROFILE = "workspace-default-v1"
_APPARMOR_PROFILE_SHA256 = "3ecb22510eba0c5458a10985ac3b150273b213f9983f9a43882fca52a67ed2cd"
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_RESERVED = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/run", "/tmp", "/tools", *_MASKS)


def _under(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _container_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value == "/":
        raise ValueError("Expected a non-root absolute container path")
    if str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts or any(c in value for c in ",:\n\r\0"):
        raise ValueError("Container path is not canonical")
    return value


def _real_directory(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute source directory")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Source directory has a symlink component")
    if not path.is_dir():
        raise ValueError("Source directory is unavailable")
    if any(c in str(path) for c in ",:\n\r\0"):
        raise ValueError("Source directory cannot be represented safely in mount options")
    return path.resolve(strict=True)


def rewrite_environment(entries, original, destination):
    result = []
    for entry in entries:
        key, equal, value = entry.partition("=")
        if not equal or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
            raise ValueError("Invalid actor environment entry")
        parts = value.split(":")
        if original != destination and any(not _under(part, original) and re.search(re.escape(original) + r"(?=/|$)", part) for part in parts):
            raise ValueError("Embedded workspace reference requires an explicit adapter")
        rewritten = [destination + part[len(original):] if _under(part, original) else part for part in parts]
        updated = ":".join(rewritten)
        result.append(key + "=" + updated)
    return result


class Sandbox:
    def __init__(self, spec, output_dir, run_id, workspace_path=None, files=None):
        self.spec = copy.deepcopy(spec)
        self.output = Path(output_dir).absolute()
        self.run_id = str(run_id)
        self.workspace = _container_path(workspace_path or spec["workspace"])
        if any(_under(self.workspace, p) or _under(p, self.workspace) for p in _RESERVED):
            raise ValueError("Workspace overlaps a protected container path")
        self.original_workspace = _container_path(spec["workspace"])
        self.seed = _real_directory(spec["seed_path"])
        if ".." in self.output.parts or any(p.is_symlink() for p in (self.output, *self.output.parents)):
            raise ValueError("Output has a noncanonical or symlink component")
        if _under(str(self.output), str(self.seed)) or _under(str(self.seed), str(self.output)):
            raise ValueError("Output and immutable seed must not contain each other")
        self.output.mkdir(parents=True, exist_ok=True)
        self.output = _real_directory(self.output)
        self.root = self.output / "sandbox"
        self.root.mkdir(exist_ok=False)
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.name = "caus_campaign_20261006_" + re.sub(r"[^a-z0-9_.-]", "_", self.run_id.lower())[:90] + "_" + uuid.uuid4().hex[:10]
        self.files = copy.deepcopy(files or [])
        self.counter = 0
        self.started = False
        self.created = False
        self.mounted = False
        self.closed = False
        self.lock = threading.RLock()
        self.repo = self.root / "merged"
        self.absolute_files = []
        self.uid = int(spec.get("actor_uid", 65532))
        if self.uid <= 0:
            raise ValueError("Actor must be unprivileged")
        self.record = {"schema": "caus.sandbox.v1", "run_id": self.run_id, "container": self.name,
                       "seed_path": str(self.seed), "workspace": self.workspace,
                       "original_workspace": self.original_workspace, "compatibility_aliases": [],
                       "apparmor_profile": _APPARMOR_PROFILE,
                       "apparmor_profile_sha256": _APPARMOR_PROFILE_SHA256, "helper_sha256": _SOURCE_SHA256,
                       "seed_binding": {"spec_sha256": hashlib.sha256(json.dumps(self.spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                                        "base_commit": self.spec.get("base_commit"), "archives": copy.deepcopy(self.spec.get("archives", []))},
                       "created_at": time.time(), "operations": [], "state": "new"}
        self._save()

    def _save(self):
        path = self.root / "STATE.json"
        temporary = path.with_suffix(".json.pending")
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        temporary.replace(path)

    def _execute(self, argv, timeout=90, tag="operation", text_limit=1048576):
        self.counter += 1
        prefix = self.commands / f"{self.counter:05d}_{tag}"
        stdout, stderr = prefix.with_suffix(".stdout"), prefix.with_suffix(".stderr")
        started = time.monotonic()
        timed_out = False
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
        result = {"argv": list(argv), "returncode": process.returncode, "timeout": timed_out,
                  "elapsed_seconds": time.monotonic() - started,
                  "stdout_path": str(stdout), "stderr_path": str(stderr),
                  "stdout_bytes": stdout.stat().st_size, "stderr_bytes": stderr.stat().st_size}
        for label, path in (("stdout", stdout), ("stderr", stderr)):
            with path.open("rb") as stream:
                result[label] = stream.read(text_limit).decode("utf-8", errors="replace")
            result[label + "_truncated"] = result[label + "_bytes"] > text_limit
        prefix.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        self.record["operations"].append({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
        self._save()
        return result

    def _workspace(self):
        for name in ("upper", "work", "merged"):
            (self.root / name).mkdir()
        os.chown(self.root / "upper", self.uid, self.uid)
        (self.root / "upper").chmod(self.seed.stat().st_mode & 0o777)
        options = f"lowerdir={self.seed},upperdir={self.root / 'upper'},workdir={self.root / 'work'}"
        result = self._execute(["mount", "-t", "overlay", "overlay", "-o", options, str(self.repo)], 30, "mount")
        if result["returncode"] == 0 or os.path.ismount(self.repo):
            self.mounted = True
            self.record["workspace_method"] = "overlayfs"
            self.record["mount_reconciled_after_timeout"] = bool(result["timeout"])
        else:
            self.repo = self.root / "reflink_workspace"
            result = self._execute(["cp", "-a", "--reflink=always", str(self.seed), str(self.repo)], 180, "reflink")
            if result["returncode"] == 0:
                self.record["workspace_method"] = "verified_reflink"
            elif self.spec.get("allow_plain_copy_for_pilot") is True:
                self.repo = self.root / "pilot_copy_workspace"
                result = self._execute(["cp", "-a", "--reflink=never", str(self.seed), str(self.repo)], 180, "pilot_copy")
                if result["returncode"]:
                    raise RuntimeError("Pilot workspace copy failed")
                self.record["workspace_method"] = "explicit_pilot_plain_copy"
            else:
                raise RuntimeError("Neither overlayfs nor reflink is available; full copies require explicit pilot scope")
        self.record["workspace_host_path"] = str(self.repo)
        self._save()

    def _write_relative(self, relative, content, mode):
        path = PurePosixPath(relative)
        if path.is_absolute() or str(path) != relative or not path.parts or any(p in (".", "..") for p in path.parts):
            raise ValueError("Cue file needs a canonical relative path")
        descriptor = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parts[:-1]:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.chown(component, self.uid, self.uid, dir_fd=descriptor, follow_symlinks=False)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            file_descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=descriptor)
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content.encode("utf-8"))
                os.fchmod(stream.fileno(), mode)
                if mode != 0:
                    os.fchown(stream.fileno(), self.uid, self.uid)
        finally:
            os.close(descriptor)

    def _cue_files(self):
        cues = self.root / "cues"
        cues.mkdir()
        rows = []
        for index, item in enumerate(self.files):
            path, content, mode = item["path"], item["content"], item.get("mode", 0o644)
            if not isinstance(content, str) or type(mode) is not int or mode < 0 or mode > 0o777:
                raise ValueError("Invalid cue content or permissions")
            if path.startswith("/"):
                path = _container_path(path)
                forbidden = (*_RESERVED, self.workspace, "/eval.sh", "/opt/gomodcache", "/cache")
                if any(_under(path, p) or _under(p, path) for p in forbidden):
                    raise ValueError("Cue mount overlaps a protected path")
                source = cues / f"{index:04d}"
                source.write_text(content)
                source.chmod(mode)
                self.absolute_files.append((source, path))
            else:
                self._write_relative(path, content, mode)
            rows.append({"path": path, "mode": mode, "bytes": len(content.encode()),
                         "sha256": hashlib.sha256(content.encode()).hexdigest()})
        self.record["cue_files"] = rows

    def start(self):
        if self.created or self.closed:
            raise RuntimeError("Sandbox cannot be started twice")
        try:
            inspected = self._execute(["docker", "image", "inspect", self.spec["image"]], 30, "image")
            if inspected["returncode"]:
                raise RuntimeError("Task image unavailable")
            image = json.loads(Path(inspected["stdout_path"]).read_text())[0]
            if "@sha256:" not in self.spec["image"] or self.spec["image"] not in image.get("RepoDigests", []):
                raise ValueError("Task image must match an immutable repository digest")
            if image.get("Config", {}).get("Volumes"):
                raise ValueError("Anonymous image volumes require explicit review")
            self.record["image_id"] = image["Id"]
            self._workspace()
            self._cue_files()
            hidden = self.root / "hidden_file"
            hidden.touch(mode=0o000)
            masks = {p: "ro,nosuid,nodev,size=65536,mode=000" for p in _MASKS}
            masks.update(self.spec.get("tmpfs_masks", {}))
            for target, options in masks.items():
                _container_path(target)
                if _under(self.workspace, target) or _under(target, self.workspace):
                    raise ValueError("Mask overlaps the workspace")
                if not {"ro", "nosuid", "nodev", "mode=000"}.issubset(options.split(",")):
                    raise ValueError("Runtime masks must be inaccessible and read-only")
            argv = ["docker", "create", "--name", self.name, "--hostname", "workspace",
                    "--label", "undertest.campaign=caus_campaign_20261006", "--network", "none", "--read-only",
                    "--user", f"{self.uid}:{self.uid}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--security-opt", "apparmor=" + _APPARMOR_PROFILE,
                    "--memory", str(self.spec["memory_bytes"]), "--memory-swap", str(self.spec["memory_bytes"]),
                    "--cpus", str(self.spec["cpus"]), "--pids-limit", str(self.spec["pids_limit"]),
                    "--shm-size", "64k", "--log-driver", "none",
                    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1073741824,mode=1777",
                    "--mount", f"type=bind,src={self.repo},dst={self.workspace}",
                    "--mount", f"type=bind,src={hidden},dst=/eval.sh,readonly", "--workdir", self.workspace]
            for target, options in masks.items():
                argv.extend(["--tmpfs", target + ":" + options])
            for entry in rewrite_environment(self.spec.get("actor_env", []), self.original_workspace, self.workspace):
                argv.extend(["--env", entry])
            tool = Path(self.spec["task_root"]) / "rg"
            if tool.exists():
                if tool.is_symlink() or not tool.is_file():
                    raise ValueError("Tool binding must be a regular pinned file")
                argv.extend(["--mount", f"type=bind,src={tool},dst=/tools/rg,readonly"])
            if self.spec.get("module_path"):
                modules = _real_directory(self.spec["module_path"])
                argv.extend(["--mount", f"type=bind,src={modules},dst=/opt/gomodcache,readonly",
                             "--tmpfs", "/cache:rw,exec,nosuid,nodev,size=2147483648,mode=0777"])
            for source, target in self.absolute_files:
                argv.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
            argv.extend(["--entrypoint", "/bin/sleep", self.spec["image"], "86400"])
            self.created = True
            created = self._execute(argv, 60, "create")
            if created["returncode"]:
                raise RuntimeError("Container creation failed; see retained controller evidence")
            self.started = True
            started = self._execute(["docker", "start", self.name], 60, "start")
            if started["returncode"]:
                raise RuntimeError("Container startup failed; see retained controller evidence")
            inspected = self._execute(["docker", "inspect", self.name], 30, "inspect")
            if inspected["returncode"] or json.loads(Path(inspected["stdout_path"]).read_text())[0].get("AppArmorProfile") != _APPARMOR_PROFILE:
                raise RuntimeError("Required workspace metadata confinement is not active")
            self.record["state"] = "running"
            self._save()
            return copy.deepcopy(self.record)
        except Exception:
            self.close()
            raise

    def run(self, command, timeout=90):
        with self.lock:
            return self._run(command, timeout)

    def _run(self, command, timeout):
        if not self.started or self.closed:
            raise RuntimeError("Sandbox is not running")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and "\0" not in x for x in command):
            raise ValueError("Command must be an argument list")
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError("Tool timeout is out of bounds")
        result = self._execute(["docker", "exec", self.name, "timeout", "-s", "TERM", "-k", "2", str(timeout), *command], timeout + 15, "tool")
        result["timeout"] = result["timeout"] or result["returncode"] in (124, 137)
        if result["elapsed_seconds"] >= timeout + 14 and result["timeout"]:
            self._execute(["docker", "stop", "-t", "2", self.name], 20, "timeout_stop")
            self.started = False
        return result

    def checkpoint(self, name="early"):
        """Pause briefly for a coherent filesystem snapshot, then resume the same actor."""
        with self.lock:
            if not self.started or self.closed or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Checkpoint requires a running sandbox and a simple name")
            directory = self.root / "checkpoints" / name
            directory.mkdir(parents=True, exist_ok=False)
            record = {"schema": "caus.sandbox_checkpoint.v1", "name": name, "at": time.time(),
                      "seed_path": str(self.seed), "workspace_method": self.record["workspace_method"],
                      "seed_binding": copy.deepcopy(self.record["seed_binding"]),
                      "helper_sha256": _SOURCE_SHA256, "apparmor_profile": _APPARMOR_PROFILE,
                      "apparmor_profile_sha256": _APPARMOR_PROFILE_SHA256,
                      "workspace": self.workspace, "archives": [], "complete": False,
                      "process_state_restoration_supported": False, "errors": []}
            pause_attempted = False
            try:
                record["git_diff_before_pause"] = self._execute(["docker", "exec", self.name, "git", "diff", "--binary", "HEAD"], 60, "checkpoint_diff")
                pause_attempted = True
                result = self._execute(["docker", "pause", self.name], 30, "pause")
                if result["returncode"]:
                    raise RuntimeError("Container could not be paused for a coherent snapshot")
                inspected = self._execute(["docker", "inspect", "--format", "{{json .State}}", self.name], 30, "paused_state")
                state = json.loads(inspected["stdout"])
                if inspected["returncode"] or not state.get("Paused") or type(state.get("Pid")) is not int or state["Pid"] <= 0:
                    raise RuntimeError("Paused container identity is unavailable")
                source = self.root / "upper" if self.mounted else self.repo
                snapshots = [("workspace_upper" if self.mounted else "workspace_full", source)]
                for target in (["/tmp", "/cache"] if self.spec.get("module_path") else ["/tmp"]):
                    snapshots.append(("tmpfs_" + target[1:], Path("/proc") / str(state["Pid"]) / "root" / target[1:]))
                for label, source in snapshots:
                    archive = directory / (label + ".tar")
                    result = self._execute(["tar", "--xattrs", "--xattrs-include=*", "--acls", "--numeric-owner", "-C", str(source), "-cf", str(archive), "."], 180, "checkpoint_" + label)
                    if result["returncode"] or result["timeout"]:
                        raise RuntimeError("Checkpoint archive failed: " + label)
                    digest = hashlib.sha256()
                    with archive.open("rb") as stream:
                        for block in iter(lambda: stream.read(1048576), b""):
                            digest.update(block)
                    record["archives"].append({"role": label, "path": str(archive), "bytes": archive.stat().st_size, "sha256": digest.hexdigest()})
                record["complete"] = True
            except Exception as exc:
                record["errors"].append({"type": type(exc).__name__, "message": str(exc)})
            finally:
                if pause_attempted:
                    try:
                        result = self._execute(["docker", "unpause", self.name], 30, "unpause")
                        record["unpause_returncode"] = result["returncode"]
                        if result["returncode"]:
                            record["complete"] = False
                            record["errors"].append({"stage": "unpause", "returncode": result["returncode"]})
                    except Exception as exc:
                        record["complete"] = False
                        record["errors"].append({"stage": "unpause", "type": type(exc).__name__, "message": str(exc)})
                record["ended_at"] = time.time()
                (directory / "CHECKPOINT.json").write_text(json.dumps(record, indent=2) + "\n")
                self.record.setdefault("checkpoints", []).append({"name": name, "record": str(directory / "CHECKPOINT.json"), "complete": record["complete"]})
                self._save()
            return record

    def close(self):
        with self.lock:
            return self._close()

    def _close(self):
        if self.closed:
            return copy.deepcopy(self.record)
        errors = []
        if self.created:
            try:
                result = self._execute(["docker", "inspect", "--format", "{{json .State}}", self.name], 30, "closure_state")
                if result["returncode"] == 0:
                    observed = json.loads(result["stdout"])
                    self.started = bool(observed.get("Running"))
                    if observed.get("Paused"):
                        result = self._execute(["docker", "unpause", self.name], 30, "closure_unpause")
                        if result["returncode"]:
                            errors.append({"stage": "closure_unpause", "returncode": result["returncode"]})
                else:
                    errors.append({"stage": "closure_state", "returncode": result["returncode"]})
            except Exception as exc:
                errors.append({"stage": "closure_state", "error_type": type(exc).__name__})
        if self.started:
            for tag, command in (("git_status", ["git", "status", "--porcelain=v1", "--untracked-files=all"]),
                                 ("git_diff", ["git", "diff", "--binary", "HEAD"])):
                try:
                    self._execute(["docker", "exec", self.name, *command], 60, tag)
                except Exception as exc:
                    errors.append({"stage": tag, "error_type": type(exc).__name__})
            for target in (["/tmp", "/cache"] if self.spec.get("module_path") else ["/tmp"]):
                try:
                    result = self._execute(["docker", "exec", "--user", "0:0", self.name, "tar", "-C", target, "-cf", "-", "."], 120, "tmpfs_" + target[1:], text_limit=0)
                    self.record.setdefault("tmpfs_snapshots", []).append({"path": target, "archive": result["stdout_path"], "returncode": result["returncode"], "timeout": result["timeout"], "consistency": "best_effort_diagnostic_while_running"})
                except Exception as exc:
                    errors.append({"stage": "tmpfs_snapshot", "error_type": type(exc).__name__})
        if self.created:
            for tag, argv in (("stop", ["docker", "stop", "-t", "2", self.name]), ("wait", ["docker", "wait", self.name])):
                try:
                    result = self._execute(argv, 20, tag)
                    if result["returncode"]:
                        errors.append({"stage": tag, "returncode": result["returncode"]})
                except Exception as exc:
                    errors.append({"stage": tag, "error_type": type(exc).__name__})
        self.mounted = self.mounted or os.path.ismount(self.repo)
        if self.mounted and not errors:
            result = self._execute(["umount", str(self.repo)], 30, "unmount")
            if result["returncode"]:
                errors.append({"stage": "unmount", "returncode": result["returncode"]})
            else:
                self.mounted = False
        self.closed = not errors
        self.record.update(state="closed" if not errors else "closure_requires_review", closure_errors=errors,
                           retained_container=self.created, overlay_mounted=self.mounted, ended_at=time.time(),
                           process_state_restoration_supported=False)
        self._save()
        return copy.deepcopy(self.record)
