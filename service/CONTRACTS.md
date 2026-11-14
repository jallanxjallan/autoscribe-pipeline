# Current Rust Implementation Contract

Rust reads and writes vault Markdown directly. Format conversion and publishing are
external to the AutoScribe service. There is no resident daemon or SQLite client ledger.

## Dispatch and tracking

- Select repository, unqueued commit, plan type, then plan, using the
  domain-independent embedded skim selector. Human display text is not parsed.
- Explicit `--commit` / `--plan` and interactive selection reach `run_commit`.
- Dispatch repository discovery is independent of `$PWD`; `--repo` selects an
  explicit repository. Writeback requires `$PWD` itself to be the Git working-tree
  root and never widens a nested current directory to its containing repository.
- Dispatch changed Markdown blobs from the selected commit's first-parent delta;
  a root commit uses its full tree. Optional paths narrow the source set.
- Validate frontmatter slugs, uniqueness, nonblank bodies and published plan identity.
  No ai-process scan, prefix convention, plan inference, or Pandoc is involved.
- Preserve body bytes apart from the existing leading directive extraction.
- Preserve PipelineCall fields and the `asc enqueue` boundary. Store repository,
  source commit and source blob in the existing `extra.context` object.
- Query existing calls/results/export custody via `asc storage dispatch-state`;
  account for queued Redis calls not yet persisted. Do not create state in Git.
- Do not create a missing ledger during read-only selection. Cancellation performs
  no enqueue, ledger write, or repository mutation.

`ResponseAction::Export` retains its serialized `export` spelling for server routing
compatibility. There is no local document-export command.

## Writers

Existing-note writeback preserves the original frontmatter bytes and replaces only
body content. Leading response frontmatter is discarded, never merged; a response slug differing from the source is rejected. Body-only
notes receive no synthesized frontmatter. Malformed leading envelopes fail before
writing. `writeback` requires `$PWD` itself to be the Git working-tree root; it
refuses nested directories instead of broadening write authority to a parent repository.
Dispatch state contains only inflight commits and busy sources. Required deliveries
remain inflight until receipted; successful action `none` does not require delivery.

Repository/commit-scoped `asc export list-pending --ndjson` returns call identity,
source identity, content and dispatch extra metadata without receipts. Snapshot
blob/slug checks, safe paths, symlink refusal and source-or-target equality guard
filesystem writes. A clean index is required; only accepted paths are committed.
One response commit carries exact `Autoscribe-Call:` trailers. Export receipts are
appended only after verifying the committed response, including repository, source
commit, writeback commit and target path in existing receipt columns. Missing
receipts recover from reachable matching trailers and target blobs. Blocked rows
remain pending and do not block independent responses. No legacy manifest writeback
path, new database schema, daemon invocation or automatic synchronization exists.

`writenew` accepts the existing NDJSON envelope. `input_record.slug` is the
source identity and becomes `source_slug` provenance. It may be omitted when
`filename_hint` is present. A slug in supplied frontmatter must match the
source assertion. Destination assertions are rejected. The output identity is
the new file's canonical `slug`; the CLI keeps its existing empty stdout.

`--target-dir` names the vault root (the current directory by default).
`_tools/config.yaml` supplies the relative passage route. The CLI invokes
`/usr/bin/python3 -B -m biznet_common.identity`, so the matching Common package
must be available to that interpreter. It stages all files, syncs their data,
and publishes each complete file exclusively. Identical filename replays fail
without changing existing files. Batch publication is not a crash-atomic
transaction across filenames.

`AscClient` owns the `asc export list-pending` and `update-exports` subprocesses.
Writeback requires no separate result extraction.
