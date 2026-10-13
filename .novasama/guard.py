"""Disable upstream workflow files using the Actions API, without editing git."""

import os
import time
from pathlib import PurePosixPath

from common import CONFIG, ROOT, github_api, run


def guard():
    repo = os.environ.get("GITHUB_REPOSITORY", CONFIG["repository"])
    workflows = {}
    page = 1
    while True:
        items = github_api(f"repos/{repo}/actions/workflows?per_page=100&page={page}")["workflows"]
        workflows.update({item["path"]: item for item in items})
        if len(items) < 100:
            break
        page += 1
    files = run("git", "ls-tree", "-r", "--name-only", "HEAD", ".github/workflows", cwd=ROOT).stdout.splitlines()
    files += list(workflows)
    for path in sorted(set(files)):
        file = PurePosixPath(path)
        if file.parent.as_posix() != ".github/workflows" or file.name.startswith("novasama-"):
            continue
        if file.suffix not in (".yaml", ".yml"):
            continue
        workflow = workflows.get(path)
        identifier = str(workflow["id"]) if workflow else file.name
        if workflow and workflow.get("state") == "disabled_manually":
            print(f"Already disabled {path}: disabled_manually", flush=True)
            continue
        for attempt in range(5):
            result = run("gh", "workflow", "disable", identifier, "--repo", repo, check=False)
            if result.returncode == 0:
                break
            # A concurrent sync/release guard may have disabled it first.
            if "403" in result.stderr:
                current = github_api(f"repos/{repo}/actions/workflows/{identifier}")
                if current["state"] == "disabled_manually":
                    break
            if "404" not in result.stderr or attempt == 4:
                raise RuntimeError(f"Could not disable {path}: {result.stderr}")
            time.sleep(3)
        state = github_api(f"repos/{repo}/actions/workflows/{identifier}")["state"]
        if state != "disabled_manually":
            raise RuntimeError(f"Unexpected workflow state for {path}: {state}")
        print(f"Disabled {path}: {state}", flush=True)


if __name__ == "__main__":
    guard()
