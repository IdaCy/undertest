import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from shared.runs import write_json


ROOT = Path(__file__).resolve().parents[3]
REVISION = re.compile(r"[0-9a-f]{40}")


def load_checkpoint(path: Path, checkpoint_id: str) -> tuple[dict, str]:
    raw = path.read_bytes()
    manifest = json.loads(raw)
    checkpoints = manifest["checkpoints"]
    ids = [row["checkpoint_id"] for row in checkpoints]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate checkpoint IDs in lineage manifest")
    selected = [row for row in checkpoints if row["checkpoint_id"] == checkpoint_id]
    if len(selected) != 1:
        raise ValueError(f"Unknown checkpoint_id: {checkpoint_id}")
    checkpoint = selected[0]
    for field in ("model_revision", "tokenizer_revision"):
        if not REVISION.fullmatch(checkpoint.get(field, "")):
            raise ValueError(f"{field} must be an immutable 40-character revision")
    for field in ("model_id", "tokenizer_id", "stage", "lineage"):
        if not checkpoint.get(field):
            raise ValueError(f"Checkpoint requires {field}")
    return checkpoint, hashlib.sha256(raw).hexdigest()


def load_panel(path: Path) -> tuple[dict, str]:
    from emergence.checkpoints.choice import ASSAY_VERSION
    from emergence.checkpoints.choice import FRESH_ASSAY_VERSION
    from emergence.checkpoints.choice import validate_choice_namespace

    raw = path.read_bytes()
    panel = json.loads(raw)
    if panel.get("schema_version") != 1 or not panel.get("assay_version"):
        raise ValueError("Panel requires schema_version=1 and assay_version")
    if panel["assay_version"] not in (ASSAY_VERSION, FRESH_ASSAY_VERSION):
        raise ValueError("Unsupported panel assay version")
    samples = panel.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("Panel must have a nonempty samples list")
    seen = set()
    for sample in samples:
        if not isinstance(sample.get("id"), str) or not sample["id"] or sample["id"] in seen:
            raise ValueError("Sample IDs must be nonempty unique strings")
        seen.add(sample["id"])
        if not isinstance(sample.get("input"), str) or not sample["input"]:
            raise ValueError("Sample input must be a nonempty string")
        metadata = sample.get("metadata", {})
        for field in ("condition", "family", "instance_id", "replicate", "seed"):
            if field not in metadata:
                raise ValueError(f"Sample {sample['id']} lacks {field}")
        if type(metadata["seed"]) is not int or not 0 <= metadata["seed"] < 2**31:
            raise ValueError("Generation seed must be an integer in [0, 2**31)")
        if validate_choice_namespace(metadata) != panel["assay_version"]:
            raise ValueError("Panel and sample assay namespaces differ")
    if panel["assay_version"] == FRESH_ASSAY_VERSION:
        from emergence.checkpoints.fresh_choice import register_panel
        register_panel(panel)
    elif (
        any(field in panel for field in ("registry", "registry_id", "registry_sha256"))
        or panel.get("panel_kind") not in (None, "calibration", "diagnostic", "smoke")
        or any(field in panel.get("build_config", {}) for field in ("rule_split", "cue_variants", "rule_ids"))
    ):
        raise ValueError("Legacy panel contains fresh registry namespace fields")
    return panel, hashlib.sha256(raw).hexdigest()


def source_files(root: Path = ROOT) -> list[Path]:
    files = [root / "pyproject.toml", root / "uv.lock"]
    for directory in ("src", "data", "tests"):
        files.extend(
            path for path in (root / directory).rglob("*")
            if path.is_file() and not path.is_symlink()
            and "__pycache__" not in path.parts
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )
    return sorted(path for path in files if path.exists())


def source_hashes(files: list[Path], root: Path = ROOT) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def save_snapshot(output: Path, hashes: dict[str, str], root: Path = ROOT) -> None:
    if (output / "source.zip").exists():
        with zipfile.ZipFile(output / "source.zip") as archive:
            actual = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()}
        if actual != hashes:
            raise ValueError("Saved source archive differs from run manifest")
        return
    pending = output / "source.zip.tmp"
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, expected in hashes.items():
            raw = (root / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Source changed while taking snapshot; use a new output directory")
            archive.writestr(relative, raw)
    pending.replace(output / "source.zip")


def git_state(root: Path = ROOT) -> dict[str, Any]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True)
    return {"commit": head.stdout.strip(), "worktree_dirty": bool(status.stdout), "status": status.stdout}


def verify_server_receipt(path: Path, checkpoint: dict, mode: str) -> dict:
    receipt = json.loads(path.read_text())
    for field in ("model_id", "model_revision", "tokenizer_id", "tokenizer_revision"):
        if receipt.get(field) != checkpoint[field]:
            raise ValueError(f"Server receipt mismatches selected checkpoint: {field}")
    for field in ("served_model", "hostname", "precision", "server_version", "launch_argv", "verified_utc"):
        if not receipt.get(field):
            raise ValueError(f"Server receipt requires {field}")
    if checkpoint["model_revision"][:12] not in receipt["served_model"]:
        raise ValueError("Served model alias must contain the first 12 characters of its revision")
    if mode == "native-chat":
        expected_templates = {
            row.get("template_sha256", row.get("sha256"))
            for row in (checkpoint.get("chat_template_files") or [])
        }
        if receipt.get("chat_template_sha256") not in expected_templates:
            raise ValueError("Native chat requires the pinned template hash in the server receipt")
    return receipt


def save_json_once(path: Path, value: Any) -> None:
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"Saved artifact changed: {path.name}")
    else:
        write_json(path, value)
