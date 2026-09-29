import argparse
from pathlib import Path

from undertest.results import summarize


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    summarize(args.run / "logs", args.run)
