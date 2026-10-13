"""Build missing listed versions and publish artifacts only to orphan gh-pages."""

import argparse
import copy
import html
import os
import shutil
import tempfile
from pathlib import Path

from build import Builder
from common import (
    CHART_NAME,
    CONFIG,
    HELM,
    ROOT,
    VERSION,
    digest,
    git_identity,
    load_yaml,
    run,
    write_yaml,
)


def select_versions(configured, index, version="", force=False):
    if force and not version:
        raise RuntimeError("Force requires an explicit version")
    if version and version not in configured:
        raise RuntimeError(f"Version {version} must first be listed in .novasama/charts.yaml")
    if len(configured) != len(set(configured)) or any(not VERSION.fullmatch(v) for v in configured):
        raise RuntimeError("Invalid or duplicate configured chart versions")
    published = {str(entry["version"]) for entry in index.get("entries", {}).get(CHART_NAME, [])}
    requested = [version] if version else configured
    for item in requested:
        if item in published and not force:
            print(f"Immutable version already published, skipping {item}", flush=True)
    return [item for item in requested if force or item not in published]


def update_index(pages, previous, rebuilt):
    args = [HELM, "repo", "index", pages, "--url", CONFIG["pages_url"]]
    if (pages / "index.yaml").exists():
        args += ["--merge", pages / "index.yaml"]
    run(*args)
    index = load_yaml(pages / "index.yaml")
    # Helm reindexes all local tgz files. Preserve existing immutable entries,
    # including timestamps, unless this version was explicitly forced.
    entries = index.setdefault("entries", {}).setdefault(CHART_NAME, [])
    preserved = {str(old["version"]): old for old in previous.get("entries", {}).get(CHART_NAME, [])
                 if str(old["version"]) not in rebuilt}
    entries[:] = [copy.deepcopy(preserved.get(str(entry["version"]), entry)) for entry in entries]
    for entry in entries:
        archive = pages / f'{CHART_NAME}-{entry["version"]}.tgz'
        if not archive.exists() or digest(archive) != entry["digest"]:
            raise RuntimeError(f'Index digest mismatch or missing archive: {entry["version"]}')
    write_yaml(pages / "index.yaml", index)


def page_html(index):
    links = "\n".join(f'<li><a href="releases/{html.escape(str(entry["version"]))}.md">'
                      f'{html.escape(str(entry["version"]))}</a></li>'
                      for entry in index.get("entries", {}).get(CHART_NAME, []))
    return ("<!doctype html>\n<html lang=\"en\"><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Novasama nxs-universal-chart</title>\n"
            "<h1>Novasama nxs-universal-chart</h1>\n"
            '<p><a href="index.yaml">Helm repository index</a></p>\n'
            "<pre>helm repo add nuc-fork https://novasamatech.github.io/nxs-universal-chart</pre>\n"
            f"<h2>Release Notes</h2><ul>\n{links}\n</ul></html>\n")


def release(publish=False, version="", force=False, output=None, cache=None):
    if publish and run("git", "branch", "--show-current", cwd=ROOT).stdout.strip() != "main":
        raise RuntimeError("Publishing must run from main")
    with tempfile.TemporaryDirectory(prefix="novasama-release-") as temp:
        temp = Path(temp)
        pages = temp / "gh-pages"
        result = run("git", "ls-remote", "--exit-code", "--heads", "origin", "gh-pages", cwd=ROOT, check=False)
        existing = result.returncode == 0
        if result.returncode not in (0, 2):
            raise RuntimeError(f"Cannot inspect gh-pages: {result.stderr}")
        if existing:
            run("git", "fetch", "--no-tags", "origin", "gh-pages:refs/remotes/origin/gh-pages", cwd=ROOT)
            shared = run("git", "merge-base", "HEAD", "refs/remotes/origin/gh-pages", cwd=ROOT, check=False)
            if shared.returncode != 1:
                raise RuntimeError("gh-pages must have no shared history with main")
            run("git", "worktree", "add", "--detach", pages, "refs/remotes/origin/gh-pages", cwd=ROOT)
        else:
            run("git", "worktree", "add", "--detach", "--no-checkout", pages, "HEAD", cwd=ROOT)
            run("git", "symbolic-ref", "HEAD", "refs/heads/gh-pages", cwd=pages)
            run("git", "read-tree", "--empty", cwd=pages)
        try:
            previous = load_yaml(pages / "index.yaml") if (pages / "index.yaml").exists() else {}
            configured = [str(item) for item in load_yaml(ROOT / ".novasama/charts.yaml")["versions"]]
            versions = select_versions(configured, previous, version, force)
            builder = Builder(cache or temp / "cache")
            packages = Path(output) if output else temp / "packages"
            for item in versions:
                builder.build(item, packages)
            if not publish:
                print(f"Build check passed for {versions}", flush=True)
                return
            notes = pages / "releases"
            notes.mkdir(exist_ok=True)
            for item in versions:
                shutil.copy2(packages / f"{CHART_NAME}-{item}.tgz", pages)
                for extension in ("md", "json"):
                    shutil.copy2(packages / f"{item}.{extension}", notes)
            if versions:
                update_index(pages, previous, set(versions))
                (pages / ".nojekyll").touch()
                (pages / "index.html").write_text(page_html(load_yaml(pages / "index.yaml")))
                git_identity(pages)
                run("git", "add", ".", cwd=pages)
                run("git", "-c", "commit.gpgsign=false", "commit", "-m",
                    "Publish self-contained charts " + ", ".join(versions), cwd=pages)
                # Normal push rejects concurrent publications; never force push.
                run("git", "push", "origin", "HEAD:refs/heads/gh-pages", cwd=pages)
                print(f"Published {versions} to orphan gh-pages", flush=True)
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                    for item in versions:
                        summary.write((notes / f"{item}.md").read_text() + "\n")
        finally:
            run("git", "worktree", "remove", "--force", pages, cwd=ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--version", default=os.environ.get("REQUESTED_VERSION", ""))
    parser.add_argument("--force", action="store_true", default=os.environ.get("FORCE_REBUILD") == "true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    release(args.publish, args.version, args.force, args.output, args.cache)
