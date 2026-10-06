import argparse
import json
from pathlib import Path

from emergence.checkpoints.choice import CONDITIONS
from emergence.checkpoints.choice import CUES
from emergence.checkpoints.choice import INSTANCE_BY_ID
from emergence.checkpoints.choice import build_panel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--conditions", choices=CONDITIONS, nargs="+")
    parser.add_argument("--instances", choices=tuple(INSTANCE_BY_ID), nargs="+")
    parser.add_argument("--cue-variant", choices=tuple(CUES), default="primary")
    parser.add_argument("--no-controls", action="store_true")
    args = parser.parse_args()
    panel = build_panel(
        repetitions=args.repetitions, seed=args.seed, conditions=args.conditions,
        include_controls=not args.no_controls, instance_ids=args.instances,
        cue_variant=args.cue_variant,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a panel that may already identify assigned episodes.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(panel, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"Wrote {len(panel['samples'])} samples to {args.output.resolve()}")


if __name__ == "__main__":
    main()
