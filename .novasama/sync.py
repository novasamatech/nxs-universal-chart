"""Preflight and merge upstream main. A dry run never mutates GitHub."""

import argparse
import os

from common import CONFIG, ROOT, git_identity, report_issue, run


def merge_check(cwd, head, upstream):
    return run("git", "merge-tree", "--write-tree", head, upstream, cwd=cwd, check=False)


def sync(dry_run=False):
    if run("git", "branch", "--show-current", cwd=ROOT).stdout.strip() != "main":
        raise RuntimeError("Sync must run on main")
    if run("git", "status", "--porcelain", cwd=ROOT).stdout.strip():
        raise RuntimeError("Sync needs a clean worktree")
    run("git", "fetch", "--no-tags", "origin", "main", cwd=ROOT)
    if not dry_run:
        run("git", "merge", "--ff-only", "origin/main", cwd=ROOT)
    run("git", "fetch", "--no-tags", CONFIG["upstream"],
        "main:refs/remotes/novasama-upstream/main", cwd=ROOT)
    upstream = run("git", "rev-parse", "refs/remotes/novasama-upstream/main", cwd=ROOT).stdout.strip()
    head = run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()
    result = merge_check(ROOT, head, upstream)
    if result.returncode:
        message = f"Upstream `{upstream}` cannot merge into fork `{head}`.\n\n```text\n{result.stdout}{result.stderr}\n```\n"
        message += "\nResolve manually in a reviewed branch; do not rebase, force-push, or auto-resolve. See .novasama/README.md.\n"
        if not dry_run:
            report_issue("Upstream main sync conflict", message)
        raise RuntimeError(message)
    print(f"Clean merge: fork {head}, upstream {upstream}, tree {result.stdout.strip()}", flush=True)
    if dry_run:
        return
    git_identity(ROOT)
    run("git", "-c", "commit.gpgsign=false", "merge", "--no-edit", upstream, cwd=ROOT)
    run("git", "push", "origin", "HEAD:refs/heads/main", cwd=ROOT)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") if os.environ.get("GITHUB_STEP_SUMMARY") else open(os.devnull, "w") as summary:
        summary.write(f"Clean upstream merge: `{upstream}`.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    sync(parser.parse_args().dry_run)
