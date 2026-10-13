"""Shared primitives for the fork's release tooling; never run chart scripts."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / ".novasama/config.yaml").read_text())
CHART_NAME = "nxs-universal-chart"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
SHA = re.compile(r"[0-9a-f]{40}")
HELM = os.environ.get("HELM_BIN", "helm")


def run(*args, cwd=None, check=True, input=None):
    args = [str(arg) for arg in args]
    if args[0] == "git":
        args[1:1] = ["-c", "core.hooksPath=/dev/null"]
    result = subprocess.run(args, cwd=cwd, input=input, text=True, check=False,
                            capture_output=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} {args[1:]} failed:\n{result.stdout}{result.stderr}")
    return result


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text()) or {}


def write_yaml(path, data):
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False))


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract(archive, destination):
    """Extract data only, rejecting links, devices, and paths outside destination."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise RuntimeError(f"Unsafe archive member: {member.name}")
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def github_api(endpoint, method="GET", data=None):
    args = ["gh", "api", endpoint, "--method", method]
    if data is not None:
        args += ["--input", "-"]
    result = run(*args, input=json.dumps(data) if data is not None else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


def git_identity(cwd):
    # Keep any existing Git identity; use the configured identity in CI.
    for field in ("name", "email"):
        if not run("git", "config", "--get", f"user.{field}", cwd=cwd, check=False).stdout.strip():
            run("git", "config", f"user.{field}", CONFIG["git_author"][field], cwd=cwd)


def report_issue(title, body):
    repo = os.environ.get("GITHUB_REPOSITORY", CONFIG["repository"])
    issues = json.loads(run("gh", "issue", "list", "--repo", repo, "--state", "open",
                            "--search", f'"{title}" in:title', "--json", "number,title").stdout)
    existing = next((issue for issue in issues if issue["title"] == title), None)
    endpoint = f"repos/{repo}/issues"
    if existing:
        endpoint += f'/{existing["number"]}'
    return github_api(endpoint, "PATCH" if existing else "POST", {"title": title, "body": body})
