import argparse
import hashlib
import json
from pathlib import Path

from emergence.checkpoints.fresh_choice import build_panel
from emergence.checkpoints.fresh_choice import register_panel
from emergence.checkpoints.fresh_registry import SPLITS
from emergence.checkpoints.fresh_registry import load_registry
from emergence.checkpoints.fresh_registry import validate_registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalogue-directory", type=Path, required=True)
    parser.add_argument("--rule-split", choices=SPLITS, required=True)
    parser.add_argument("--cue-variant", action="append", required=True)
    parser.add_argument("--rule-id", action="append")
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--no-controls", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    paths = [args.output] + ([args.report] if args.report else [])
    if len({path.resolve() for path in paths}) != len(paths) or any(path.exists() for path in paths):
        parser.error("Output paths must be distinct and must not exist")
    try:
        registry = load_registry(args.catalogue_directory)
        panel = build_panel(
            registry, repetitions=args.repetitions, seed=args.seed,
            rule_split=args.rule_split, cue_variants=args.cue_variant,
            rule_ids=args.rule_id, include_controls=not args.no_controls,
        )
        assignment = register_panel(panel)
        raw = json.dumps(panel, ensure_ascii=False, indent=2) + "\n"
        report = {
            "schema_version": 1, "assay_version": panel["assay_version"],
            "allocation_status": panel["allocation_status"],
            "panel_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "panel_bytes": len(raw.encode()), "build_config": panel["build_config"],
            "registry_validation": validate_registry(registry), "assignment": assignment,
        }
        for path, content in [(args.output, raw)] + ([(args.report, json.dumps(report, ensure_ascii=False, indent=2) + "\n")] if args.report else []):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(f"Wrote {len(panel['samples'])} samples; allocation remains unallocated: {args.output}")


if __name__ == "__main__":
    main()
