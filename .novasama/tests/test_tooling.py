import copy
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build import Builder, enabled, merge_values, normalized_manifests, sample_files
from common import CONFIG, HELM, ROOT, digest, extract, load_yaml, run, write_yaml
from guard import guard
from release import select_versions, update_index
from sync import merge_check


def chart(path, name="nxs-universal-chart", version="1.2.3", dependencies=()):
    path.mkdir(parents=True, exist_ok=True)
    write_yaml(path / "Chart.yaml", {"apiVersion": "v2", "name": name, "version": version,
                                   "dependencies": list(dependencies), "icon": "https://registry.nixys.ru/logo.png"})
    write_yaml(path / "values.yaml", {})
    return path


class ToolingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="novasama-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_archive_rejects_paths_links_and_devices(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE), ("device", tarfile.CHRTYPE)):
            with self.subTest(name=name):
                archive = self.root / "unsafe.tar"
                with tarfile.open(archive, "w") as stream:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "../outside"
                    stream.addfile(member)
                with self.assertRaisesRegex(RuntimeError, "Unsafe archive"):
                    extract(archive, self.root / "output")
        self.assertFalse((self.root / "outside").exists())

    def test_immutable_versions_and_explicit_force(self):
        configured = ["3.1.0", "3.2.1", "3.2.2"]
        index = {"entries": {"nxs-universal-chart": [{"version": "3.2.1", "digest": "untouched"}]}}
        original = copy.deepcopy(index)
        self.assertEqual(select_versions(configured, index), ["3.1.0", "3.2.2"])
        self.assertEqual(select_versions(configured, index, "3.2.1"), [])
        self.assertEqual(select_versions(configured, index, "3.2.1", True), ["3.2.1"])
        self.assertEqual(index, original)
        with self.assertRaisesRegex(RuntimeError, "explicit version"):
            select_versions(configured, index, force=True)
        with self.assertRaisesRegex(RuntimeError, "listed"):
            select_versions(configured, index, "2.8.1")

    def test_helm_conditions_and_omission_checks(self):
        values = merge_values({"dep": {"enabled": False, "remove": 1}}, {"dep": {"enabled": True, "remove": None}})
        self.assertEqual(values, {"dep": {"enabled": True}})
        self.assertTrue(enabled(values, "missing.path,dep.enabled"))
        self.assertFalse(enabled(values, "missing.enabled", default=False))
        builder = Builder(self.root / "cache", {})
        builder.records = [{"source": "omitted", "condition": "dep.enabled", "gates": ["parent.enabled"]}]
        self.assertEqual(builder.omissions_enabled(values), builder.records)
        values["parent"] = {"enabled": False}
        self.assertEqual(builder.omissions_enabled(values), [])

    def test_unconditional_missing_dependency_fails(self):
        directory = chart(self.root / "root", dependencies=[{"name": "absent", "version": "1.0.0"}])
        builder = Builder(self.root / "cache", {})
        with (patch.object(builder, "resolve", return_value={"source": "omitted", "reason": "missing"}),
              self.assertRaisesRegex(RuntimeError, "Unconditional")):
            builder.prepare(directory, "nxs-universal-chart", "1.2.3")

    def test_recursive_resolution_and_removed_functionality(self):
        directory = chart(self.root / "root", dependencies=[{"name": "parent", "version": "1.0.0", "repository": "oci://registry.nixys.ru/nuc"}])
        (directory / "Chart.lock").write_text("registry.nixys.ru\n")
        builder = Builder(self.root / "cache", {})
        def resolve(name, version, destination):
            if name == "parent":
                chart(destination, name, version, [{"name": "absent", "version": "1.0.0", "condition": "absent.enabled"}])
                (destination / "Chart.lock").write_text("old lock")
                return {"source": "tag", "version": version, "repo": "example", "commit": "a" * 40}
            return {"source": "omitted", "reason": "missing"}
        with patch.object(builder, "resolve", side_effect=resolve):
            builder.prepare(directory, "nxs-universal-chart", "1.2.3")
        root = load_yaml(directory / "Chart.yaml")
        child = load_yaml(directory / "charts/parent/Chart.yaml")
        self.assertNotIn("repository", root["dependencies"][0])
        self.assertNotIn("icon", root)
        self.assertEqual(child["dependencies"], [])
        self.assertEqual(json.loads(child["annotations"]["novasama.io/omitted-dependencies"]), ["absent@1.0.0"])
        self.assertEqual(builder.records[-1]["condition"], "parent.absent.enabled")
        self.assertEqual(list(directory.rglob("Chart.lock")), [])
        schema = json.loads((directory / "charts/parent/values.schema.json").read_text())
        self.assertEqual(schema["allOf"][0]["properties"]["absent"]["properties"]["enabled"]["not"], {"const": True})

    def test_approved_replacement_updates_only_build_metadata(self):
        directory = chart(self.root / "root", dependencies=[{"name": "dep", "version": "1.0.5", "condition": "dep.enabled"}])
        builder = Builder(self.root / "cache", {})
        def resolve(name, version, destination):
            chart(destination, name, "1.0.0")
            return {"source": "replacement", "version": "1.0.0", "resources": {"items": "Example"}}
        with patch.object(builder, "resolve", side_effect=resolve):
            builder.prepare(directory, "nxs-universal-chart", "1.2.3")
        metadata = load_yaml(directory / "Chart.yaml")
        self.assertEqual(metadata["dependencies"][0]["version"], "1.0.0")
        self.assertIn("dep@1.0.5->1.0.0", metadata["annotations"]["novasama.io/replaced-dependencies"])
        supplied = {"dep": {"enabled": True, "items": {"test": {"spec": {"value": 42}}}}}
        manifest = "kind: Example\nmetadata: {name: test}\nspec: {value: 42, other: true}\n"
        self.assertEqual(builder.validate_resources(supplied, supplied, manifest), 1)
        with self.assertRaisesRegex(RuntimeError, "dropped/changed"):
            builder.validate_resources(supplied, supplied, manifest.replace("42", "43"))
        with self.assertRaisesRegex(RuntimeError, "dropped/changed"):
            builder.validate_resources(supplied, supplied, "")

    def test_vendor_checksum_and_exact_identity(self):
        source = chart(self.root / "vendor-chart", "dep", "1.2.3")
        vendor = self.root / ".novasama/vendor"
        vendor.mkdir(parents=True)
        archive = vendor / "dep-1.2.3.tgz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(source, arcname="dep")
        builder = Builder(self.root / "cache", {"vendor": {"dep@1.2.3": {"sha256": digest(archive)}}})
        with patch("build.ROOT", self.root), patch.object(builder, "repository", return_value=None):
            result = builder.resolve("dep", "1.2.3", self.root / "resolved")
            self.assertEqual(result["source"], "vendor")
            builder.config["vendor"]["dep@1.2.3"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                builder.resolve("dep", "1.2.3", self.root / "bad")

    def test_transient_fetch_error_does_not_become_omission(self):
        builder = Builder(self.root / "cache", {})
        error = subprocess.CompletedProcess([], 128, "", "Failed to connect to github.com")
        with (patch("build.run", return_value=error),
              self.assertRaisesRegex(RuntimeError, "will not silently omit")):
            builder.repository("https://github.com/novasamatech/dep.git")

    def test_missing_fork_never_falls_back_to_upstream(self):
        builder = Builder(self.root / "cache", {})
        with patch.object(builder, "repository", return_value=None) as repository:
            result = builder.resolve("dep", "1.2.3", self.root / "resolved")
        self.assertEqual(result["source"], "omitted")
        repository.assert_called_once_with("https://github.com/novasamatech/dep.git")

    def test_chart_source_never_falls_back_to_upstream(self):
        builder = Builder(self.root / "cache", {})
        with (patch.object(builder, "repository", return_value=None) as repository,
              self.assertRaisesRegex(RuntimeError, "Chart source repository is unavailable")):
            builder.build("3.2.1", self.root / "output")
        repository.assert_called_once_with("https://github.com/novasamatech/nxs-universal-chart.git")

    def test_build_sources_are_fork_owned(self):
        self.assertEqual(CONFIG["source"], "https://github.com/novasamatech/nxs-universal-chart.git")
        self.assertEqual(CONFIG["dependency_owner"], "novasamatech")
        dependencies = load_yaml(ROOT / ".novasama/dependencies.yaml")
        for section in ("overrides", "fallbacks"):
            for key, entry in dependencies[section].items():
                with self.subTest(dependency=key):
                    self.assertTrue(entry["repo"].startswith("https://github.com/novasamatech/"))

    def test_guard_disables_only_upstream_workflow_files(self):
        workflows = {"workflows": [
            {"id": 1, "path": ".github/workflows/tests.yml"},
            {"id": 2, "path": ".github/workflows/novasama-release.yml"},
            {"id": 3, "path": "dynamic/pages/pages-build-deployment"},
        ]}
        success = subprocess.CompletedProcess([], 0, ".github/workflows/tests.yml\n", "")
        with (patch("guard.github_api", side_effect=[workflows, {"state": "disabled_manually"}]),
              patch("guard.run", return_value=success) as command):
            guard()
        disables = [call.args for call in command.call_args_list if call.args[:3] == ("gh", "workflow", "disable")]
        self.assertEqual(len(disables), 1)
        self.assertEqual(disables[0][3], "1")

    def test_guard_accepts_already_disabled_workflow(self):
        workflows = {"workflows": [{"id": 1, "path": ".github/workflows/tests.yml", "state": "disabled_manually"}]}
        success = subprocess.CompletedProcess([], 0, ".github/workflows/tests.yml\n", "")
        with (patch("guard.github_api", return_value=workflows),
              patch("guard.run", return_value=success) as command):
            guard()
        self.assertEqual(len(command.call_args_list), 1)

    def test_guard_accepts_disable_race_but_not_permission_errors(self):
        workflows = {"workflows": [{"id": 1, "path": ".github/workflows/tests.yml", "state": "active"}]}
        listed = subprocess.CompletedProcess([], 0, ".github/workflows/tests.yml\n", "")
        forbidden = subprocess.CompletedProcess([], 1, "", "HTTP 403: workflow is not active")
        with (patch("guard.github_api", side_effect=[workflows, {"state": "disabled_manually"}, {"state": "disabled_manually"}]),
              patch("guard.run", side_effect=[listed, forbidden])):
            guard()
        with (patch("guard.github_api", side_effect=[workflows, {"state": "active"}]),
              patch("guard.run", side_effect=[listed, forbidden]),
              self.assertRaisesRegex(RuntimeError, "Could not disable")):
            guard()

    def test_exact_tag_precedes_override_and_pinned_override_matches_metadata(self):
        repo = chart(self.root / "repo", "dep", "1.2.3")
        run("git", "init", "--initial-branch=main", repo)
        run("git", "config", "user.name", "Fixture", cwd=repo)
        run("git", "config", "user.email", "fixture@example.invalid", cwd=repo)
        run("git", "add", ".", cwd=repo)
        run("git", "-c", "commit.gpgsign=false", "commit", "-m", "fixture", cwd=repo)
        commit = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        run("git", "tag", "v1.2.3", cwd=repo)
        entry = {"repo": "https://github.com/novasamatech/dep.git", "commit": commit, "path": "."}
        builder = Builder(self.root / "cache", {"overrides": {"dep@1.2.3": entry}})
        with patch.object(builder, "repository", return_value=repo / ".git") as repository:
            result = builder.resolve("dep", "1.2.3", self.root / "tagged")
            self.assertEqual(result["source"], "tag")
            self.assertEqual(result["repo"], "https://github.com/novasamatech/dep.git")
            repository.assert_called_once_with("https://github.com/novasamatech/dep.git")
            run("git", "tag", "--delete", "v1.2.3", cwd=repo)
            result = builder.resolve("dep", "1.2.3", self.root / "override")
            self.assertEqual(result["source"], "override")
            builder.config["overrides"]["dep@1.2.4"] = entry
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                builder.resolve("dep", "1.2.4", self.root / "wrong")

    def test_sample_discovery_includes_legacy_docs_but_excludes_subcharts(self):
        expected = []
        for path in ("docs/samples/issuers.yml", "samples/app/values.yaml", "values.yaml.example"):
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("{}\n")
            expected.append(file)
        child = self.root / "charts/dep/samples/values.yaml"
        child.parent.mkdir(parents=True)
        child.write_text("{}\n")
        self.assertEqual(sample_files(self.root), sorted(expected))

    def test_manifest_comparison_keeps_resource_identity_and_specs(self):
        original = "kind: Pod\nmetadata: {name: pod, labels: {helm.sh/chart: universal-chart-2.8.1}}\nspec: {containers: [{name: app-abc12, image: example}]}\n"
        renamed = original.replace("universal-chart", "nxs-universal-chart").replace("abc12", "def34")
        expected = normalized_manifests(original, "universal-chart", "2.8.1")
        self.assertEqual(expected, normalized_manifests(renamed, "nxs-universal-chart", "2.8.1"))
        self.assertNotEqual(expected, normalized_manifests(renamed.replace("name: pod", "name: other"), "nxs-universal-chart", "2.8.1"))
        self.assertNotEqual(expected, normalized_manifests(renamed.replace("image: example", "image: changed"), "nxs-universal-chart", "2.8.1"))

    @unittest.skipUnless(shutil.which(HELM), "Helm is required for pinned source integration test")
    def test_pinned_legacy_build_changes_name_and_rejects_wrong_version(self):
        repo = chart(self.root / "repo", "universal-chart", "1.2.3")
        (repo / "templates").mkdir()
        (repo / "templates/configmap.yaml").write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: fixed-name\n  labels:\n    helm.sh/chart: "{{ .Chart.Name }}-{{ .Chart.Version }}"\n')
        run("git", "init", "--initial-branch=main", repo)
        run("git", "config", "user.name", "Fixture", cwd=repo)
        run("git", "config", "user.email", "fixture@example.invalid", cwd=repo)
        run("git", "add", ".", cwd=repo)
        run("git", "-c", "commit.gpgsign=false", "commit", "-m", "fixture", cwd=repo)
        commit = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        run("git", "-c", "tag.gpgSign=false", "tag", "v1.2.3", cwd=repo)
        run("git", "-c", "tag.gpgSign=false", "tag", "v1.2.4", cwd=repo)
        builder = Builder(self.root / "cache", {})
        source = {"commit": commit, "original_name": "universal-chart"}
        builder.sources = {"1.2.3": source, "1.2.4": source}
        output = self.root / "output"
        with patch.object(builder, "repository", return_value=repo / ".git"):
            report = builder.build("1.2.3", output)
            self.assertEqual(report["source_repository"], "https://github.com/novasamatech/nxs-universal-chart.git")
            self.assertEqual(report["upstream_commit"], commit)
            self.assertEqual(report["metadata_normalization_checks"], 1)
            self.assertEqual(report["metadata_changes"], [{"field": "name", "from": "universal-chart", "to": "nxs-universal-chart"}])
            extract(output / "nxs-universal-chart-1.2.3.tgz", self.root / "unpacked")
            built = self.root / "unpacked/nxs-universal-chart"
            self.assertEqual(load_yaml(built / "Chart.yaml")["name"], "nxs-universal-chart")
            self.assertEqual((built / "templates/configmap.yaml").read_bytes(), (repo / "templates/configmap.yaml").read_bytes())
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                builder.build("1.2.4", output)

    @unittest.skipUnless(shutil.which(HELM), "Helm is required for removed-functionality test")
    def test_helm_rejects_enabling_removed_dependency(self):
        directory = chart(self.root / "root", dependencies=[{"name": "absent", "version": "1.0.0", "condition": "absent.enabled"}])
        builder = Builder(self.root / "cache", {})
        with patch.object(builder, "resolve", return_value={"source": "omitted", "reason": "missing"}):
            builder.prepare(directory, "nxs-universal-chart", "1.2.3")
        self.assertEqual(run(HELM, "template", "test", directory).returncode, 0)
        result = run(HELM, "template", "test", directory, "--set", "absent.enabled=true", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/absent/enabled", result.stderr)

    def test_merge_tree_reports_real_conflict(self):
        repo = self.root / "git"
        run("git", "init", "--initial-branch=main", repo)
        run("git", "config", "user.name", "Fixture", cwd=repo)
        run("git", "config", "user.email", "fixture@example.invalid", cwd=repo)
        def commit(text):
            (repo / "Chart.yaml").write_text(text)
            run("git", "add", ".", cwd=repo)
            run("git", "-c", "commit.gpgsign=false", "commit", "-m", text.strip(), cwd=repo)
            return run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        base = commit("base\n")
        ours = commit("ours\n")
        run("git", "checkout", "-b", "upstream", base, cwd=repo)
        theirs = commit("theirs\n")
        self.assertNotEqual(merge_check(repo, ours, theirs).returncode, 0)
        self.assertEqual(merge_check(repo, base, theirs).returncode, 0)

    @unittest.skipUnless(shutil.which(HELM), "Helm is required for index integration test")
    def test_index_keeps_existing_entry_unchanged(self):
        pages = self.root / "pages"
        pages.mkdir()
        for version in ("3.1.0", "3.2.1"):
            source = chart(self.root / version, version=version)
            run(HELM, "package", source, "--destination", pages)
        run(HELM, "repo", "index", pages)
        previous = load_yaml(pages / "index.yaml")
        old = next(entry for entry in previous["entries"]["nxs-universal-chart"] if entry["version"] == "3.1.0")
        old["created"] = "2000-01-01T00:00:00Z"
        write_yaml(pages / "index.yaml", previous)
        update_index(pages, previous, {"3.2.1"})
        updated = load_yaml(pages / "index.yaml")
        preserved = next(entry for entry in updated["entries"]["nxs-universal-chart"] if entry["version"] == "3.1.0")
        self.assertEqual(old, preserved)


if __name__ == "__main__":
    unittest.main()
