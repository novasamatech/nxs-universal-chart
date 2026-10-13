"""Build self-contained charts from exported upstream trees, never from main."""

import argparse
import copy
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

import yaml
from common import (
    CHART_NAME,
    CONFIG,
    HELM,
    NAME,
    ROOT,
    SHA,
    VERSION,
    digest,
    extract,
    load_yaml,
    run,
    write_yaml,
)


def merge_values(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_values(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def at_path(values, path):
    for part in path.split(".") if path else []:
        if not isinstance(values, dict) or part not in values:
            return None
        values = values[part]
    return values


def enabled(values, condition, default=True):
    # Helm uses the first comma-separated condition that resolves to a boolean.
    for path in condition.split(","):
        value = at_path(values, path.strip())
        if isinstance(value, bool):
            return value
    return default


def prefix_condition(prefix, condition):
    return ",".join(prefix + part.strip() for part in condition.split(","))


def chart_condition(condition, prefix):
    return [part.strip().removeprefix(prefix) for part in condition.split(",")]


def clean_metadata(value):
    if isinstance(value, dict):
        return {key: clean_metadata(item) for key, item in value.items()
                if not (isinstance(item, str) and "registry.nixys.ru" in item)}
    if isinstance(value, list):
        return [clean_metadata(item) for item in value
                if not (isinstance(item, str) and "registry.nixys.ru" in item)]
    return value


def assert_chart(directory, name, version):
    chart = load_yaml(directory / "Chart.yaml")
    if chart.get("name") != name or str(chart.get("version")) != version:
        raise RuntimeError(f"Chart identity mismatch: expected {name}@{version}, got "
                           f'{chart.get("name")}@{chart.get("version")}')
    return chart


def assert_subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and assert_subset(value, actual[key]) for key, value in expected.items())
    return expected == actual


def sample_files(directory):
    samples = set(directory.glob("*values*.example"))
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if relative.parts[0] == "charts" or not path.is_file():
            continue
        if path.suffix in (".yaml", ".yml") and any(
                part in ("samples", "examples", "example") for part in relative.parts[:-1]):
            samples.add(path)
    return sorted(samples)


def normalized_manifests(manifests, original_name, version):
    def normalize(value, container=False):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "helm.sh/chart" and item == f"{original_name}-{version}":
                    result[key] = f"{CHART_NAME}-{version}"
                elif key in ("containers", "initContainers") and isinstance(item, list):
                    result[key] = [normalize(entry, container=True) for entry in item]
                elif container and key == "name" and isinstance(item, str):
                    # Legacy templates generate a random five-character suffix
                    # when a container name is omitted in values.
                    result[key] = re.sub(r"-[a-z0-9]{5}$", "-GENERATED", item)
                else:
                    result[key] = normalize(item)
            return result
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    return normalize([doc for doc in yaml.safe_load_all(manifests) if doc is not None])


class Builder:
    def __init__(self, cache, config=None):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.config = config if config is not None else load_yaml(ROOT / ".novasama/dependencies.yaml")
        self.sources = load_yaml(ROOT / ".novasama/charts.yaml").get("sources", {})
        self.example_overrides = {}
        self.repositories = {}
        self.records = []

    def repository(self, url):
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", url):
            raise RuntimeError(f"Only public GitHub HTTPS repositories are allowed: {url}")
        if url in self.repositories:
            return self.repositories[url]
        directory = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".git")
        if directory.exists():
            if run("git", "--git-dir", directory, "remote", "get-url", "origin").stdout.strip() != url:
                raise RuntimeError(f"Unexpected cached repository: {directory}")
            run("git", "--git-dir", directory, "fetch", "--quiet", "--tags", "origin")
        else:
            result = run("git", "-c", "credential.helper=", "clone", "--quiet", "--bare", url,
                         directory, check=False)
            if result.returncode:
                if "could not read Username" in result.stderr or "Repository not found" in result.stderr:
                    self.repositories[url] = None
                    return None
                raise RuntimeError(f"Repository fetch failed (will not silently omit): {result.stderr}")
        self.repositories[url] = directory
        return directory

    def export(self, repository, commit, path, destination):
        path = PurePosixPath(path)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Invalid chart path: {path}")
        with tempfile.TemporaryDirectory(prefix="novasama-export-") as temp:
            archive = Path(temp) / "tree.tar"
            run("git", "--git-dir", repository, "archive", "--format=tar", "--output", archive, commit)
            extract(archive, Path(temp) / "tree")
            source = Path(temp) / "tree" / path
            shutil.copytree(source, destination)

    def pinned(self, entry, name, version, destination, kind):
        if not SHA.fullmatch(str(entry["commit"])):
            raise RuntimeError("Dependency overrides/replacements need a full commit SHA")
        repository = self.repository(entry["repo"])
        if repository is None:
            raise RuntimeError(f'Pinned repository unavailable: {entry["repo"]}')
        run("git", "--git-dir", repository, "cat-file", "-e", entry["commit"] + "^{commit}")
        self.export(repository, entry["commit"], entry["path"], destination)
        assert_chart(destination, name, version)
        return {"source": kind, "repo": entry["repo"], "commit": entry["commit"],
                "path": entry["path"], "version": version}

    def resolve(self, name, version, destination):
        key = f"{name}@{version}"
        url = f"https://github.com/nixys/{name}.git"
        repository = self.repository(url)
        if repository is not None:
            for tag in ("v" + version, version):
                result = run("git", "--git-dir", repository, "rev-parse", "--verify",
                             f"refs/tags/{tag}^{{commit}}", check=False)
                if result.returncode:
                    continue
                commit = result.stdout.strip()
                metadata = run("git", "--git-dir", repository, "show", f"{commit}:Chart.yaml",
                               check=False)
                chart = yaml.safe_load(metadata.stdout) if metadata.returncode == 0 else {}
                if chart.get("name") != name or str(chart.get("version")) != version:
                    continue
                expected = self.config.get("history_audit", {}).get("tagged", {}).get(key)
                if expected and expected != commit:
                    raise RuntimeError(f"Audited tag moved: {key}: {expected} -> {commit}")
                self.export(repository, commit, ".", destination)
                return {"source": "tag", "repo": url, "commit": commit, "tag": tag, "version": version}
        entry = self.config.get("overrides", {}).get(key)
        if entry:
            return self.pinned(entry, name, version, destination, "override")
        entry = self.config.get("vendor", {}).get(key)
        if entry:
            archive = ROOT / ".novasama/vendor" / f"{name}-{version}.tgz"
            if digest(archive) != entry["sha256"]:
                raise RuntimeError(f"Vendored archive checksum mismatch: {key}")
            with tempfile.TemporaryDirectory(prefix="novasama-vendor-") as temp:
                extract(archive, temp)
                shutil.copytree(Path(temp) / name, destination)
            assert_chart(destination, name, version)
            return {"source": "vendor", "sha256": entry["sha256"], "version": version}
        entry = self.config.get("fallbacks", {}).get(key)
        if entry:
            result = self.pinned(entry, name, str(entry["version"]), destination, "replacement")
            result["resources"] = entry["resources"]
            return result
        reason = "No exact tag, pinned override, vendored archive, or approved replacement"
        if repository is None:
            reason = "No public repository, pinned override, or vendored archive"
        return {"source": "omitted", "reason": reason}

    def prepare(self, directory, name, version, prefix="", gates=(), stack=()):
        if (name, version) in stack:
            raise RuntimeError(f"Cyclic dependency: {name}@{version}")
        chart = assert_chart(directory, name, version)
        shutil.rmtree(directory / "charts", ignore_errors=True)
        (directory / "Chart.lock").unlink(missing_ok=True)
        kept = []
        local_records = []
        for dependency in chart.get("dependencies", []):
            dependency = copy.deepcopy(dependency)
            dep_name, dep_version = dependency["name"], str(dependency["version"])
            alias = dependency.get("alias", dep_name)
            if not NAME.fullmatch(dep_name) or not NAME.fullmatch(alias) or not VERSION.fullmatch(dep_version):
                raise RuntimeError(f"Dependency must use an exact name/version: {dependency}")
            (directory / "charts").mkdir(exist_ok=True)
            destination = directory / "charts" / dep_name
            source = self.resolve(dep_name, dep_version, destination)
            condition = prefix_condition(prefix, dependency["condition"]) if dependency.get("condition") else ""
            record = {"name": dep_name, "requested_version": dep_version, "values_path": prefix + alias,
                      "condition": condition, "gates": list(gates), **source}
            self.records.append(record)
            local_records.append(record)
            if source["source"] == "omitted":
                if not condition:
                    raise RuntimeError(f"Unconditional dependency unavailable: {dep_name}@{dep_version}")
                continue
            dependency.pop("repository", None)
            dependency["version"] = source["version"]
            kept.append(dependency)
            child_gates = gates + ((condition,) if condition else ())
            self.prepare(destination, dep_name, source["version"], prefix + alias + ".", child_gates,
                         stack + ((name, version),))
        chart["dependencies"] = kept
        chart = clean_metadata(chart)
        annotations = chart.setdefault("annotations", {})
        omitted = [f'{r["name"]}@{r["requested_version"]}' for r in local_records if r["source"] == "omitted"]
        replacements = [f'{r["name"]}@{r["requested_version"]}->{r["version"]}'
                        for r in local_records if r["source"] == "replacement"]
        if omitted:
            annotations["novasama.io/omitted-dependencies"] = json.dumps(omitted)
        if replacements:
            annotations["novasama.io/replaced-dependencies"] = json.dumps(replacements)
        write_yaml(directory / "Chart.yaml", chart)
        if omitted:
            schema_path = directory / "values.schema.json"
            schema = json.loads(schema_path.read_text()) if schema_path.exists() else {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
            constraints = schema.setdefault("allOf", [])
            for record in local_records:
                if record["source"] != "omitted":
                    continue
                for condition in chart_condition(record["condition"], prefix):
                    constraint = {"not": {"const": True}, "description": f'{record["name"]} is unavailable and removed from this package'}
                    for part in reversed(condition.split(".")):
                        constraint = {"properties": {part: constraint}}
                    constraints.append(constraint)
            schema_path.write_text(json.dumps(schema, indent=2) + "\n")

    def defaults(self, directory):
        # Helm consumes the first YAML document. Some upstream defaults contain
        # document-end markers followed by documentation fragments.
        path = directory / "values.yaml"
        loader = yaml.SafeLoader(path.read_text() if path.exists() else "")
        try:
            values = loader.get_data() or {}
        finally:
            loader.dispose()
        for dep in load_yaml(directory / "Chart.yaml").get("dependencies", []):
            key = dep.get("alias", dep["name"])
            child = self.defaults(directory / "charts" / dep["name"])
            values[key] = merge_values(child, values.get(key, {}) or {})
            values[key]["global"] = merge_values(values[key].get("global", {}) or {}, values.get("global", {}) or {})
        return values

    def omissions_enabled(self, values):
        return [record for record in self.records if record["source"] == "omitted"
                and all(enabled(values, gate) for gate in record["gates"])
                and enabled(values, record["condition"], default=False)]

    def validate_resources(self, supplied, merged, manifests):
        documents = [doc for doc in yaml.safe_load_all(manifests) if isinstance(doc, dict)]
        checked = 0
        for record in self.records:
            if record["source"] != "replacement" or not all(enabled(merged, gate) for gate in record["gates"]):
                continue
            if record["condition"] and not enabled(merged, record["condition"]):
                continue
            values = at_path(supplied, record["values_path"]) or {}
            for collection, kind in record["resources"].items():
                for key, item in (values.get(collection) or {}).items():
                    if not isinstance(item, dict) or item.get("enabled") is False:
                        continue
                    resource_name = str(item.get("name", key))
                    matches = [doc for doc in documents if doc.get("kind") == kind
                               and str(doc.get("metadata", {}).get("name")) == resource_name
                               and assert_subset(item.get("spec", {}), doc.get("spec", {}))]
                    if not matches:
                        raise RuntimeError(f"Replacement dropped/changed {record['name']} {kind}/{resource_name}")
                    checked += 1
        return checked

    def validate(self, directory, samples_root=None, run_samples=True, baseline=None):
        for chart_file in sorted(directory.rglob("Chart.yaml")):
            chart = load_yaml(chart_file)
            result = run(HELM, "dependency", "list", chart_file.parent)
            rows = [line.split() for line in result.stdout.splitlines()[1:] if line.strip()]
            if len(rows) != len(chart.get("dependencies", [])) or any(row[-1] not in ("ok", "unpacked") for row in rows):
                raise RuntimeError(f"Dependencies are not present:\n{result.stdout}{result.stderr}")
            if "registry.nixys.ru" in chart_file.read_text() or chart_file.with_name("Chart.lock").exists():
                raise RuntimeError(f"Forbidden chart metadata: {chart_file}")
        run(HELM, "lint", directory)
        defaults = self.defaults(directory)
        if self.omissions_enabled(defaults):
            raise RuntimeError("Chart defaults enable an omitted dependency")
        result = run(HELM, "template", "novasama-test", directory)
        if baseline:
            self.compare_normalization(baseline, result.stdout)
        if not run_samples:
            return []
        samples_root = samples_root or directory
        samples = sample_files(samples_root)
        results = []
        for sample in sorted(samples):
            supplied = load_yaml(sample)
            relative = str(sample.relative_to(samples_root))
            values_args = ["--values", sample]
            override = self.example_overrides.get(relative)
            if override:
                if digest(sample) != override["sha256"]:
                    raise RuntimeError(f"Example override checksum mismatch: {relative}")
                supplied = merge_values(supplied, override["values"])
                for key, value in override["values"].items():
                    if not NAME.fullmatch(key):
                        raise RuntimeError("Example overrides require top-level values keys")
                    values_args += ["--set-json", f"{key}={json.dumps(value)}"]
            merged = merge_values(defaults, supplied)
            omitted = self.omissions_enabled(merged)
            entry = {"path": relative}
            if override:
                entry["example_override"] = override["reason"]
            if omitted:
                entry.update(status="skipped", omitted=[r["name"] for r in omitted])
            else:
                result = run(HELM, "template", "novasama-test", directory, *values_args)
                if baseline:
                    self.compare_normalization(baseline, result.stdout, values_args)
                checked = self.validate_resources(supplied, merged, result.stdout)
                entry.update(status="passed", replacement_resources_checked=checked)
            results.append(entry)
            print(f'{entry["status"]}: {entry["path"]}', flush=True)
        return results

    def compare_normalization(self, baseline, actual, values_args=()):
        args = [HELM, "template", "novasama-test", baseline]
        args += list(values_args)
        expected = run(*args).stdout
        metadata = load_yaml(baseline / "Chart.yaml")
        version = str(metadata["version"])
        if normalized_manifests(expected, metadata["name"], version) != normalized_manifests(actual, CHART_NAME, version):
            raise RuntimeError(f"Metadata normalization changed resources: {values_args or 'defaults'}")

    def build(self, version, output):
        if not VERSION.fullmatch(version):
            raise RuntimeError(f"Invalid chart version: {version}")
        self.records = []
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        repository = self.repository(CONFIG["upstream"])
        if repository is None:
            raise RuntimeError("Upstream repository is unavailable")
        tag = "v" + version
        tag_commit = run("git", "--git-dir", repository, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").stdout.strip()
        source = self.sources.get(version)
        self.example_overrides = source.get("example_overrides", {}) if source else {}
        commit = source["commit"] if source else tag_commit
        if not SHA.fullmatch(commit):
            raise RuntimeError("Chart source pins need a full commit SHA")
        original_name = source["original_name"] if source else CHART_NAME
        metadata_changes = []
        with tempfile.TemporaryDirectory(prefix="novasama-build-") as temp:
            directory = Path(temp) / CHART_NAME
            self.export(repository, commit, ".", directory)
            metadata = assert_chart(directory, original_name, version)
            baseline = None
            if original_name != CHART_NAME:
                baseline = Path(temp) / "original"
                shutil.copytree(directory, baseline)
                metadata["name"] = CHART_NAME
                metadata.setdefault("annotations", {}).update({
                    "novasama.io/original-chart-name": original_name,
                    "novasama.io/upstream-commit": commit,
                })
                write_yaml(directory / "Chart.yaml", metadata)
                metadata_changes.append({"field": "name", "from": original_name, "to": CHART_NAME})
            self.prepare(directory, CHART_NAME, version)
            self.validate(directory, run_samples=False)
            run(HELM, "package", directory, "--destination", output)
            package = output / f"{CHART_NAME}-{version}.tgz"
            # Validate what Helm actually put in the archive, including .helmignore.
            extract(package, Path(temp) / "packaged")
            packaged = Path(temp) / "packaged" / CHART_NAME
            if baseline:
                source_files = {str(path.relative_to(baseline)): path.read_bytes()
                                for path in baseline.rglob("*") if path.is_file()
                                and (path.relative_to(baseline).parts[0] == "templates" or path.name == "values.yaml")}
                packaged_files = {str(path.relative_to(packaged)): path.read_bytes()
                                  for path in packaged.rglob("*") if path.is_file()
                                  and (path.relative_to(packaged).parts[0] == "templates" or path.name == "values.yaml")}
                if source_files != packaged_files:
                    raise RuntimeError("Legacy templates/defaults changed during packaging")
            samples = self.validate(packaged, samples_root=directory, baseline=baseline)
            report = {"version": version, "upstream_tag": tag, "upstream_commit": commit,
                      "upstream_tag_commit": tag_commit,
                      "source_kind": "pinned-commit" if source else "tag",
                      "metadata_changes": metadata_changes,
                      "metadata_normalization_checks": len(samples) + 1 if baseline else 0,
                      "sha256": digest(package), "dependencies": self.records, "samples": samples}
            notes = [f"# {CHART_NAME} {version}", "", f"Upstream tag: `{tag}` at `{tag_commit}`.",
                     f"Built public upstream commit: `{commit}`.",
                     f"Package SHA256: `{report['sha256']}`.", "", "## Dependencies", ""]
            for record in self.records:
                identity = f'{record["name"]}@{record["requested_version"]}'
                if record["source"] == "omitted":
                    source = f'OMITTED ({record["condition"]}): {record["reason"]}'
                elif record["source"] == "vendor":
                    source = f'vendored SHA256 `{record["sha256"]}`'
                else:
                    source = f'{record["repo"]}@{record["commit"]} (path `{record.get("path", ".")}`)'
                    if record["source"] == "replacement":
                        source = f'REPLACED with {record["version"]}: ' + source
                notes.append(f"- `{identity}`: {source}")
            if not self.records:
                notes.append("No external chart dependencies.")
            if metadata_changes:
                notes += ["", "## Metadata", "", f"Chart name normalized from `{original_name}` to `{CHART_NAME}`.",
                          "Templates and values are unchanged from the pinned public tree.",
                          "This is a source rebuild; equivalence to an original binary archive is not claimed.",
                          f"Defaults and {len(samples)} examples retain their resources except the chart label."]
            passed = sum(s["status"] == "passed" for s in samples)
            checked = sum(s.get("replacement_resources_checked", 0) for s in samples)
            notes += ["", "## Validation", "", "Helm dependency list, lint, defaults and packaged render passed.",
                      f"Samples: {passed} passed; {len(samples) - passed} skipped (omitted dependencies).",
                      f"Replacement resource name/kind/spec checks: {checked}."]
            for sample in samples:
                if sample["status"] == "skipped":
                    notes.append(f'- Skipped `{sample["path"]}`: {", ".join(sample["omitted"])}')
                if sample.get("example_override"):
                    notes.append(f'- Validation override for `{sample["path"]}`: {sample["example_override"]}')
            (output / f"{version}.md").write_text("\n".join(notes) + "\n")
            (output / f"{version}.json").write_text(json.dumps(report, indent=2) + "\n")
            return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="novasama-cache-") as temporary:
        Builder(args.cache or temporary).build(args.version, args.output)
