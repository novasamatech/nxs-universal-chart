"""Archive only the published orphan branch for the Pages deployment."""

import argparse
from pathlib import Path

from common import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    run("tar", "--create", "--file", args.archive, "--directory", args.source,
        "--exclude=.git", ".")
