"""Summarize saved calibration exports without rescoring or model operations."""

import argparse
from pathlib import Path

from emergence.checkpoints.analysis import write_analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, required=True, help="Full original assigned panel, never the continuation subset")
    parser.add_argument("--run", type=Path, action="append", required=True, help="Original run first, then continuation run(s); repeat this flag")
    parser.add_argument("--reconciliation", type=Path, help="Interruption reconciliation, required with continuation runs")
    parser.add_argument("--output", type=Path, required=True, help="New analysis directory outside all source runs")
    args = parser.parse_args()
    try:
        summary = write_analysis(args.panel, args.run, args.output, reconciliation_path=args.reconciliation, cli_source=Path(__file__))
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    print(f"Wrote {summary['assigned_count']} assigned episodes ({summary['measurement_available_count']} measured) to {args.output.resolve()}")


if __name__ == "__main__":
    main()
