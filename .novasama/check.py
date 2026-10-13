"""PR check: build all listed versions, even if they are already published."""

import tempfile
from pathlib import Path

from build import Builder
from common import ROOT, load_yaml

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="novasama-check-") as temp:
        temp = Path(temp)
        builder = Builder(temp / "cache")
        for version in load_yaml(ROOT / ".novasama/charts.yaml")["versions"]:
            builder.build(str(version), temp / "packages")
