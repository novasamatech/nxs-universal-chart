# Novasama chart repository

Public Helm endpoint: <https://novasamatech.github.io/nxs-universal-chart>.
Packages use the chart name `nxs-universal-chart` and exact chart versions.

## Layout and releases

Fork-owned files on `main` are new files under `.novasama/` and workflows named
`.github/workflows/novasama-*.yml`. Upstream tracked files are never edited,
moved, or deleted on `main`. Upstream merges use full Git history and ordinary
merge commits, never rebases or force pushes.

An orphan `gh-pages` branch stores `index.yaml`, packages, `.nojekyll`,
`index.html`, and per-version `releases/<version>.md` and `.json` provenance.
It is never merged into `main`. The release workflow deploys that branch through
**GitHub Actions**. An explicit
deploy job is necessary because pushes using `GITHUB_TOKEN` do not trigger
other workflows. The Pages upload contains only the published branch.

`novasama-sync.yml` runs hourly and can be dispatched manually. It preflights
`git merge-tree --write-tree`, merges the exact fetched upstream main SHA,
pushes normally, disables upstream workflows through the Actions API, and
explicitly dispatches the release workflow. Conflicts fail and create or update
the issue `Upstream main sync conflict`. Its dry-run input checks the merge and
guard without pushing or publishing.

`novasama-release.yml` runs on changes under `.novasama/`, manual dispatch,
and reusable workflow calls. A repository-wide concurrency group serializes
publication. A version already in the index is skipped without modifying its
archive, notes, digest, or index entry. Manual `force: true` requires one explicit
listed version and is the only workflow path that can rebuild it. A rerun also
retries Pages deployment if publication previously succeeded but deployment failed.

`novasama-check.yml` builds all listed versions on relevant PRs without publishing.
PR jobs have read-only permissions and never use `pull_request_target`.

CI runs Helm and our own scripts, never chart Makefiles, scripts, Git hooks,
or pre-commit hooks. All third-party action references are full commit SHAs;
Helm 3.19.0 is checksum-pinned. Python 3 and the Ubuntu runner's system PyYAML
are used to parse structured metadata; no pip install or chart code is executed.
Only `GITHUB_TOKEN` is used in workflows. Git commits retain an existing Git
identity, or use the configured Git identity in `config.yaml` when CI has none.

## Dependency resolution

Builds export public upstream tags or explicitly pinned commits into temporary
directories and assert source chart name/version. The `sources` entries in
`charts.yaml` pin 2.5.1 and 2.8.1 to public upstream trees with exact versions.
Their chart name is normalized from `universal-chart` to `nxs-universal-chart`
in the build copy. Defaults and examples are compared before and after this
normalization; only `helm.sh/chart` label values may change. Existing generated
container-name suffixes are normalized during comparison. Templates and default
values are also compared byte-for-byte. Source and metadata
changes are recorded in provenance. Checksum-pinned example overlays affect
validation inputs only and are recorded in source configuration and notes.
Dependency resolution recurses and tries:

1. `nixys/<name>` tag `v<version>` or `<version>` with exact chart metadata.
2. An exact `name@version` override with a full commit SHA and chart path.
3. A checksum-pinned `.novasama/vendor/<name>-<version>.tgz` original.
4. An explicitly configured replacement in `dependencies.yaml`.
5. Remove an unresolved conditional dependency; fail for an unconditional one.

Replacements are never inferred automatically. The following replacements use pinned public
`1.0.0` trees, and both requested and actual versions appear in notes, JSON
provenance, and `novasama.io/replaced-dependencies` annotations:

| Requested dependency | Packaged version |
| --- | --- |
| nuc-traefik 1.0.5 | 1.0.0 |
| nuc-certificates 1.0.4 | 1.0.0 |
| nuc-fluxcd 1.0.4 | 1.0.0 |
| nuc-knative 1.0.5 | 1.0.0 |
| nuc-kserve 1.0.4 | 1.0.0 |
| nuc-kube-prometheus-stack 1.0.5 | 1.0.0 |
| nuc-victoria-metrics 1.0.4 | 1.0.0 |
| nuc-keda 1.0.4 | 1.0.0 |

All upstream samples are rendered. For replaced dependencies, explicitly supplied
resources must retain their kind, name, and supplied spec fields. This validates
the shipped example workflows, not equivalence with unavailable upstream versions
or compatibility with every cluster's CRDs. Custom values require separate validation.

The 14 exact tagged dependencies are nuc-common, nuc-istio, nuc-argocd,
nuc-native-gateway, nuc-vault-secret-operator, nuc-cloudnativepg,
nuc-mysql-percona-operator, nuc-rabbitmq, nuc-clickhouse, nuc-elk,
nuc-external-secrets, nuc-mongodb-percona-operator, nuc-envoy-gateway, and
nuc-valkey. Their exact SHAs and the completed public history audit are recorded
in `dependencies.yaml`.

Removed functionality: **nuc-keycloak-operator@1.0.1** and
**nuc-strimzi-kafka-operator@1.0.0** have no public repositories. They are removed
from packaged dependency metadata and listed in
`novasama.io/omitted-dependencies` and release notes. The packaged values schema
rejects enabling them, preventing a successful render that silently drops resources.
Do not skip schema validation to enable an unavailable dependency.

In build copies only, dependencies are unpacked under `charts/`, dependency
repository fields and `Chart.lock` are removed recursively, and metadata pointing
to `registry.nixys.ru` (including icons) is removed. Helm dependency list, lint,
default rendering, samples that do not enable omitted dependencies, and unpacked
package checks must pass. Templates and supported values retain their source content.
Helm consumes the first document in values files; our defaults inspection does
the same, including the public Envoy Gateway defaults' documentation after `...`.

## Add a version

Add an exact version to `.novasama/charts.yaml`, commit, and push to `main`.
The workflow resolves `nixys/nxs-universal-chart` tag `v<version>` unless the
version has a pinned source entry. Unexpected source names or versions fail.
Only an explicitly configured source name can be normalized.

```sh
gh workflow run novasama-release.yml --repo novasamatech/nxs-universal-chart --ref main
gh workflow run novasama-release.yml --repo novasamatech/nxs-universal-chart --ref main -f version=3.2.1 -f force=true
```

The second command intentionally replaces an immutable release. Prefer a new
upstream chart version whenever possible. An existing packaged version stays
unchanged even if dependency configuration changes.

## Overrides and recovered archives

Inspect a dependency's full public `Chart.yaml` history once and record the
findings in `history_audit`. An override must match the exact requested metadata:

```yaml
overrides:
  nuc-example@1.2.3:
    repo: https://github.com/owner/nuc-example.git
    commit: 0123456789abcdef0123456789abcdef01234567
    path: charts/nuc-example
```

For an exact archive obtained from a public source, place
`nuc-example-1.2.3.tgz` under `.novasama/vendor/`, calculate its SHA256, and add:

```yaml
vendor:
  nuc-example@1.2.3:
    sha256: <64 lowercase hexadecimal characters>
```

Archives are checked before extraction; wrong metadata, checksum, unsafe paths,
symlinks, devices, and unresolved unconditional nested dependencies fail. Each
nested dependency follows the same resolution policy. Archives already embedded
in source trees are replaced by the explicitly resolved dependency trees.

To approve a different available version, add a `fallbacks` entry using `version`,
`repo`, full `commit`, `path`, and a `resources` map of values collections to
Kubernetes kinds. Inspect the source contracts and exercise the relevant examples
before approving it. A failed compatibility check blocks publication; remove
the fallback entry to omit the conditional feature instead.

## Sync conflicts

Open the generated issue, fetch the two tips, and reproduce the merge in a
temporary worktree. Resolve in a reviewed branch. Keep every upstream tracked
file at its upstream version; adapt fork-owned files only. If upstream adopts
one of our new paths, choose a fresh fork-owned path in the recovery change.
Merge the reviewed result into `main` without rewriting history, then dispatch
`novasama-sync.yml`. The guard always runs after sync, including a failed sync.

```sh
gh workflow run novasama-sync.yml --repo novasamatech/nxs-universal-chart --ref main -f dry_run=true
gh workflow disable tests.yml --repo novasamatech/nxs-universal-chart
```

For a newly created fork where the Actions API has not registered workflows yet,
use `[skip ci]` on the first bootstrap commit, disable upstream workflows through
the API after registration, then dispatch our release. Future user pushes should
keep upstream workflows disabled; the sync guard also handles new upstream files.

## Repository settings

Required repository settings; workflows do not change them:

- Actions enabled in this fork, allowing the pinned actions used here.
- Workflow permissions: read and write; job-level permissions remain narrowly
  scoped (guard: actions write; publisher: contents write; deploy: pages write
  and id-token write; sync: contents/actions/issues write).
- Pages source: **GitHub Actions**. Artifacts live only on `gh-pages`.
- The `github-pages` environment must allow deployments initiated from `main`.
- Issues enabled, with Actions allowed to create issues for sync conflicts and
  source-audit issues. The PR-approval checkbox is not needed.
- Check branch protection and organization rules: sync cannot bypass a protected
  `main` with `GITHUB_TOKEN`, and publication cannot bypass protected `gh-pages`.

## Local verification

Local prerequisites: Git with `merge-tree --write-tree`, Helm 3, Python 3 +
PyYAML, curl, tar, and gh authenticated only for writes/Actions administration.

```sh
/usr/bin/python3 -m unittest discover -s .novasama/tests
/usr/bin/python3 .novasama/build.py 3.2.1 --output /tmp/novasama-packages
/usr/bin/python3 .novasama/sync.py --dry-run
helm repo add nuc-fork https://novasamatech.github.io/nxs-universal-chart
helm pull nuc-fork/nxs-universal-chart --version 3.2.1
helm template offline ./nxs-universal-chart-3.2.1.tgz -f .novasama/fixtures/external-secrets.yaml
```

Use isolated `HELM_CONFIG_HOME`, `HELM_CACHE_HOME`, and `HELM_DATA_HOME` for a clean
anonymous repository check. Rendering downloaded packages needs no chart registry.

Public source information for older versions is in `legacy-versions.md`.
