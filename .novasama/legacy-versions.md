# Public sources for older versions

Verified from the public upstream Git history on 2026-10-13:

| Version | Public tag metadata | Public commit with the requested version |
| --- | --- | --- |
| 2.5.1 | v2.5.1: name universal-chart, version 2.5.1 | 8469512ba7fab4a92262813e200e58a34e073d07 |
| 2.8.1 | v2.8.1: name universal-chart, version 2.8.0 | 08aef1f3c42c1b91f0d0eba01e85a8a6f44f273a |

Repository: <https://github.com/nixys/nxs-universal-chart>.
Build source mirror: <https://github.com/novasamatech/nxs-universal-chart>.
The builder reads the pinned commits from this fork, not the upstream repository.

Both listed commits declare `name: universal-chart`. Building a package named
`nxs-universal-chart` from either tree requires an explicit metadata normalization.
The source commit and any metadata changes must be recorded in package provenance.

Public release assets, chart repository indexes, and public forks can be checked
for original archives. A rebuilt archive must not be described as byte-identical
to an original unless a public original and its digest have been verified.

## Public archive audit

- Upstream GitHub releases have no attached chart archives, including v2.5.1
  and v2.8.1.
- Public code searches for the exact archive names returned no matching files.
- The public Pages indexes of chpock, GRomR1, and evgkrsk do not list these
  two versions.
- GlacierWalrus publishes `universal-chart-2.8.1.tgz` in its public
  [release](https://github.com/GlacierWalrus/nxs-universal-chart/releases/tag/universal-chart-2.8.1).
  Its downloaded SHA256 is
  `a8a741f0fde8d99318ae872a860d5dd822a2176b71ae655f0639736d56d605a1`,
  matching the release asset metadata. Its Pages index instead records
  `737a8e5050f6b1b55cfb5bc066b282c1f473e29508f835dc8f851a0094dca70d`.
  The archive also differs from the public upstream 2.8.1 candidate in nine
  template/defaults files, so it is not used as an upstream original.

The public [Nixys article](https://nixys.hashnode.dev/nxs-universal-chart-helm-chart-to-simplify-kubernetes-operations)
includes the chart label `nxs-universal-chart-2.8.1`. This is public evidence for
the normalized chart name, not evidence that a particular rebuilt archive
matches a previously published binary.

## Source reconstruction

`charts.yaml` pins the upstream commits listed above. Only the chart name is
changed in build copies; the version, templates, and values retain their pinned
source content. These trees have no external chart dependencies.

The 2.5.1 candidate and tag differ only in README/changelog content. The 2.8.1
candidate includes the subsequent SealedSecrets commit; it is distinct from tag
v2.8.1, which still declares 2.8.0. Provenance lists the tag commit and built
commit separately. This is a reproducible reconstruction from public source,
not a claim of equivalence to an original registry archive.

The public `docs/samples/issuers.yml` example has an empty `generic` block that
parses as null and fails the source templates. Validation uses `--set-json
generic={}` for this checksum-pinned example only. This is recorded in notes;
packaged templates and default values are unchanged.
