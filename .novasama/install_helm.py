"""Install the checksum-pinned Helm binary; no downloaded installer scripts."""

import os
import tempfile
from pathlib import Path

from common import digest, extract, run

VERSION = "3.19.0"
SHA256 = "a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496"


def main():
    destination = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "novasama-bin"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novasama-helm-") as temp:
        archive = Path(temp) / "helm.tar.gz"
        run("curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
            f"https://get.helm.sh/helm-v{VERSION}-linux-amd64.tar.gz", "--output", archive)
        if digest(archive) != SHA256:
            raise RuntimeError("Helm archive checksum mismatch")
        extract(archive, Path(temp) / "unpacked")
        binary = destination / "helm"
        binary.write_bytes((Path(temp) / "unpacked/linux-amd64/helm").read_bytes())
        binary.chmod(0o755)
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a") as stream:
            stream.write(str(destination) + "\n")
    print(binary)


if __name__ == "__main__":
    main()
