import fcntl
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4


SCHEMA_VERSION = 1
IDENTITY_FIELDS = {"schema_version", "run_id", "config_id"}


def config_id(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    pending = path.with_name(path.name + ".tmp")
    pending.write_text(json.dumps(value, indent=2) + "\n")
    pending.replace(path)


def run_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"undertest": {
        key: manifest[key]
        for key in ("run_id", "config_id", "model_revision", "section", "run_type")
    }}


@contextmanager
def run_directory(output: Path, config: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if IDENTITY_FIELDS & config.keys():
        raise ValueError("Run identity fields must not be supplied as configuration.")
    # Normalize tuples and other JSON-compatible containers before comparing resumes.
    config = json.loads(json.dumps(config, allow_nan=False))
    identity = config_id(config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"Another runner owns {output}; use another output directory.") from exc
        try:
            manifest_path = output / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    if manifest["schema_version"] != SCHEMA_VERSION:
                        raise ValueError("Unsupported manifest schema")
                    UUID(manifest["run_id"])
                    saved_config = {k: v for k, v in manifest.items() if k not in IDENTITY_FIELDS}
                    if saved_config != config or manifest["config_id"] != identity:
                        raise ValueError("Configuration or sources changed")
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise ValueError(
                        "This output directory has an incompatible manifest, configuration, "
                        "or source version; choose a new --output."
                    ) from exc
            else:
                if any(p.name != ".run.lock" for p in output.iterdir()):
                    raise ValueError("Output directory contains unregistered results; choose a new --output.")
                manifest = {
                    **config,
                    "schema_version": SCHEMA_VERSION,
                    "run_id": uuid4().hex,
                    "config_id": identity,
                }
                write_json(manifest_path, manifest)
            yield manifest
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
