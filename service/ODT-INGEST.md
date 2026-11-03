# Original ODT book ingest

Implemented in `extensions/scripts/odt_ingest.py`, with a creation-only book adapter
in the existing Rust `service` writer. No daemon, model call, editorial processing,
Drive mutation, or automatic publishing is involved.

## Environment checked on Biznet, 2026-11-03

SSH alias `biznet` works as jeremy. Noninteractive SSH does not include
`/home/jeremy/.local/bin` on PATH; use absolute binaries or export that path.
Pandoc 3.12.1 lists the ODT reader; automated tests exercise actual ODT conversion.
`gdrive:` is configured, but a bounded root listing returns Google HTTP 403
RATE_LIMIT_EXCEEDED for shared rclone client project 202264815644. Drive read access,
source-file discovery and a real-manuscript smoke test remain unverified. Credentials
were not changed. Rclone also warns that its shared client ID is being retired.

Source checkout: `/home/jeremy/pipeline`, GitHub
`jallanxjallan/autoscribe-pipeline`, server remote `/home/jeremy/Repos/pipeline.git`.
The separate `/home/jeremy/services` checkout handles queue/dispatch/writeback
services; its `vault-tools` synchronizes editing resources, not manuscripts.
The relevant creation writer is `service/src/operations/writenew.rs`. It accepts
NDJSON `{input_record: {...scalar metadata...}, content: "Markdown"}`, serializes
metadata as quoted YAML, and exclusively creates files. It previously required
caller-supplied identities and had no book-manifest generator.

The small book adapter preserves that default contract. `writenew --book-title`
accepts the same envelopes with exactly `source` and `chapter` metadata, checks the
whole input, assigns canonical `chp_` / `pub_` 80-bit Crockford identities, adds
`type`, `status: draft`, `created`, `updated`, and produces an ordered flat canonical-slug wikilink
manifest. Publication defaults are `type: manifest`, `pipeline: typst`, `output: pdf`.
These follow the authoritative type-based chapter/publication templates in
`/home/jeremy/Templates/obsidian/content` and the existing slug helper in
`/home/jeremy/Tools/obsidian/tools/scripts/templater/slug.js`, with the user's
confirmation that type-based templates are authoritative. Creation dates use the
server's local date, matching the template helper's local-date convention.

## Invocation

Build without installing or deploying:

```sh
cd /home/jeremy/pipeline/service
/home/jeremy/.cargo/bin/cargo build --bin writenew
export PATH=/home/jeremy/.local/bin:$PATH
```

Choose an existing **empty** destination directory. The report must be outside it
and must not already exist. Substitute the actual Drive path; names below are
examples, not verified source locations.

```sh
python3 /home/jeremy/pipeline/extensions/scripts/odt_ingest.py \
  'gdrive:Books/Jakarta Jive.odt' \
  --book-title 'Jakarta Jive' --target-dir /absolute/empty/destination \
  --writer /home/jeremy/pipeline/service/target/debug/writenew \
  --report /tmp/jakarta-preview.json --dry-run
```

Remove `--dry-run` and use a new report filename to create the book. Substitute
Bali Blues and its actual path to ingest that book. The reference is an rclone
Drive path: `lsjson --stat` resolves its stable ID, which becomes `source` in every
chapter. The original is copied only to temporary storage and statted again to
reject an ID/size/modification-time change during retrieval.

Dry run downloads and parses the source and writes the explicitly requested report,
but writes nothing to the vault. Rust prints prospective paths and complete records;
preview identities are provisional and regenerated on real creation. A complete
report retains chapter bodies, titles, document metadata and indexed non-chapter AST
blocks. Treat reports as manuscript content.

Only explicit headings of the selected level, titled `Chapter 1`, `Chapter 2`, etc.,
with an optional colon/dash/period and title, are accepted. Numbering must start at
1 and be consecutive. `--heading-level` selects another verified heading level.
Roman numerals, word numbers, unstyled boundary candidates, empty chapters,
non-chapter headings/material and unsupported content require manual review; they
never trigger guesses or partial editorial transformations. Non-chapter blocks
before, between and after chapters are retained in the report. Unmarked end matter
cannot be distinguished from chapter prose: inspect the report before applying.

Pandoc retains ordinary prose, dialogue, emphasis, quotations, footnotes and visible
scene separators. Empty ODT paragraphs are rejected because their scene-break meaning
cannot be inferred. Tables, images, embedded objects, annotations, tracked changes,
non-pagination headers/footers, raw blocks and other unsupported AST nodes fail
explicitly. Pagination-only headers/footers are layout, not manuscript content.
This deliberately conservative first version does not claim full ODT feature support.

Rust creates `Chapter 0001.md`, `Chapter 0002.md`, ... and `Book.md` in the supplied
folder. That folder must be empty even in dry run. A repeated run fails rather than
overwriting, including after manual edits or renames. Exclusive file creation also
refuses races/symlinks. As with existing writenew, an I/O failure during materialization
can leave a partial new book; inspect it and choose a new empty destination to retry.
Nothing is committed or synchronized in the destination automatically.

## Verification

```sh
cd /home/jeremy/pipeline/extensions/scripts
PATH=/home/jeremy/.local/bin:$PATH python3 -m unittest test_odt_ingest -v
cd /home/jeremy/pipeline/service
/home/jeremy/.cargo/bin/cargo test operations::book --lib
/home/jeremy/.cargo/bin/cargo test operations::writenew --lib
```

Python tests generate a small genuine ODT fixture with Pandoc and read it back,
checking two ordered chapters, exact source fields, dialogue, italics, quotations,
footnotes, scene separators, missing/ambiguous boundaries and non-chapter blocks.
Rust tests check canonical output, whole-input validation, dry-run writes, flat
manifest links and refusal to overwrite edited content.

## Verified generated example

Disposable fixture output (identities vary):

```markdown
---
"chapter": 1
"created": "2026-11-03"
"filename_hint": "Chapter 0001"
"slug": "chp_TJWN857YAG6NQMTX"
"source": "fixture_Drive_ID"
"status": "draft"
"type": "chapter"
"updated": "2026-11-03"
---
# Chapter 1

"Original dialogue," she said.

*Italic words*

> A quotation.

\* \* \*

Text[^1]

[^1]: Footnote text.
```

Manifest:

```markdown
---
"created": "2026-11-03"
"filename_hint": "Book"
"output": "pdf"
"pipeline": "typst"
"slug": "pub_SEPS4N7SF93CK9X8"
"status": "draft"
"type": "manifest"
"updated": "2026-11-03"
---
# @title Fixture Book

[[chp_TJWN857YAG6NQMTX]]

[[chp_H02TD9VV0GHGHE9B]]
```

## Results on 2026-11-03

- Python unittest: 4 passed, including genuine ODT round trip and annotation refusal.
- Rust `operations::book`: 1 passed (canonical metadata, dry run, validation and edited retry refusal).
- Rust `operations::writenew`: 1 passed (existing contract/destination regression).
- `cargo build --bin writenew`, `cargo fmt --check`, and `git.py diff --check`: passed.
- Disposable end-to-end fixture smoke: ODT -> Pandoc AST -> source-only NDJSON ->
  Rust writer -> installed shared manifest parser and publication normalizer.
  Confirmed exact output filenames, canonical slug link order, one published title,
  no vault output in dry run, and refusal to overwrite a manually corrected chapter.
  No PDF was rendered and no production service was deployed. The normalizer was
  exercised read-only from its existing installation; ingest has no Guide dependency.
- Real Drive manuscript smoke: blocked by HTTP 403 quota error, not claimed successful.

Implementation is on dedicated branch `odt-back-catalogue-ingest`; no push or merge.

## Public-link real-data test

The positional reference now also accepts an HTTPS public Google Drive file URL,
including `/file/d/ID/view`, `/open?id=ID`, and resource-key links. This path uses
Python standard-library HTTPS downloading with temporary in-memory cookies for
Google's download-confirmation form. It does not call rclone, use Drive API
credentials, or change server configuration. The file must be an original ODT
with download access, not a Google Docs conversion. The supplied Drive file ID
is retained as source. Downloads are limited to 128 MiB and checked as ODT ZIPs;
login, quota, unrecognized HTML and unsupported confirmation pages fail explicitly.
Browser download pages are a provider-controlled interface and may change.

For the first real manuscript test, use a temporary empty destination and dry run:

```sh
export PATH=/home/jeremy/.local/bin:$PATH
odt_test_dir=$(mktemp -d /tmp/odt-book-test.XXXXXX)
mkdir "$odt_test_dir/chapters"
python3 /home/jeremy/pipeline/extensions/scripts/odt_ingest.py \
  'https://drive.google.com/file/d/FILE_ID/view?usp=sharing' \
  --book-title 'BOOK TITLE' --target-dir "$odt_test_dir/chapters" \
  --writer /home/jeremy/pipeline/service/target/debug/writenew \
  --report "$odt_test_dir/preview.json" --dry-run
```

Review the report. To materialize the disposable test book, repeat the command
without `--dry-run`, using `$odt_test_dir/ingest.json` as the report filename.
No production vault is selected by these instructions. No source edits or model
calls occur. Public retrieval cannot stat an authenticated Drive revision;
it processes the downloaded bytes and identifies the source by the URL file ID.

Verification: 9 Python tests passed, including public URL parsing/resource keys,
direct and confirmation downloads, refusal of login/foreign pages and non-ODT
ZIPs, plus the existing real-ODT preservation tests. A complete CLI smoke test
with simulated public HTTP and a genuine ODT passed through the actual Rust
writer: dry run left the destination empty, real creation produced two chapters
and Book.md, source ID persisted, and retry refused a manually edited chapter.
A live public manuscript download remains pending the user's source URL.

Google's documented browser-download distinction:
https://developers.google.com/workspace/drive/api/guides/manage-downloads
