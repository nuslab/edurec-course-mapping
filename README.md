# edurec-mappings

Read-only extraction of NUS EduRec **Course Mapping Approval** requests into
a run directory of YAML files. Stage 1 only: the package collects structured evidence, optionally the
text of linked syllabus documents, and never records decisions or comments.

## Layout

```
edurec-mappings/
├── pyproject.toml            # package metadata; installs the `edurec-mappings` command
├── src/edurec_mappings/
│   ├── cli.py                # argument parsing, login prompt, browser lifecycle
│   ├── extract.py            # cap-aware search loop with YAML checkpoints
│   ├── browser.py            # EduRec navigation (search form, paging, detail, View 100)
│   ├── models.py             # dataclasses for rows, requests, linked documents and the export
│   ├── parse.py              # list/detail HTML parsing into those records
│   ├── documents.py          # download and text extraction of URLs in course details
│   ├── anonymize.py          # pseudonymised copy of an export
│   └── terms.yaml            # terms searched when --term is blank
└── tests/                    # unittest suite; fixtures/ holds trimmed EduRec pages

../edurec-data/               # runtime data, kept outside the package and out of Git
├── output/                   # run directories; contain personal data, keep private
└── browser-profile/          # persisted login state (cookies for EduRec)
```

## Setup

The devcontainer installs the package globally; rebuild the container after
changing it. For a manual install:

```sh
pip install -e '.[dev]'
python3 -m playwright install chromium
```

For live collection, open forwarded port **6080**, click **Connect**, and enter
the desktop password `vscode`. Keep this port private. The browser appears in
that desktop; do not wrap the command in `xvfb-run`.

## Run

Run from this directory; the default paths are relative to it.

```sh
edurec-mappings --reassign-id '' --term '' --rows '' --scrape-urls --anonymize \
  --output ../edurec-data/output/module-mappings
```

`python3 -m edurec_mappings` is equivalent. Log in through VNC, accept the
policy if you agree. The command
runs three steps once the approval form is visible (detected automatically after login; Enter retries at once): it applies its own search filters, switches the results grid
to **View 100**, opens every matching request and checkpoints the run
directory after each detail; with `--scrape-urls` it then fetches every URL
found in the course details and stores their text under `documents/`; with
`--anonymize` it finally writes a pseudonymised copy of the run next to it.

| Argument | Supplied | Blank or omitted |
| --- | --- | --- |
| `--reassign-id` | Exact, case-insensitive match on the ReassignID column, filtered locally | All reassignees, including unassigned |
| `--term` | One four-digit term code, e.g. `2610` | Every term in `terms.yaml` (override with `--terms-file`) |
| `--rows` | Stop after this many unique requests | All matching requests |
| `--scrape-urls` | Fetch URLs in course details and store their text | `linked_documents` stays `null` |
| `--anonymize` | Also write `<output>-anonymized/` (or `--anonymized-output`) | Original only |
| `--cdp-url URL --ready` | Attach to a running, logged-in Chromium; left open on exit | Launch a browser on `--profile` and prompt for login |

`--help` lists the rest (`--profile`, `--proxy`, `--timeout`).

## Why searches are subdivided

EduRec displays at most 300 rows per search and paging cannot reach the rest.
Observed on 2026-09-21: a blank-term search showed 300 rows, term `2610` showed
293, and term `2620` alone was capped. The extractor therefore searches each
configured term separately and, when a search is capped, splits it by student
ID, then mapping group and sequence. Inclusive boundaries can overlap, so
requests are deduplicated by full identity. A search that stays capped after
every split ends the run with an `interrupted` checkpoint rather than a false
claim of completeness. Results follow search order, not a global sort.

## Export

`--output` names a run directory that is both the export and the checkpoint.
It is laid out so that an AI advisor can read one request at a time instead of
the whole inventory:

```
module-mappings/
├── inventory.yaml            # collection audit, mapping_groups, list_pages
├── requests/<request_id>.yaml   # one file per unique request
├── documents/<hash>.txt      # scraped text, one file per URL (--scrape-urls)
└── decisions/<request_id>.yaml  # written by the course-mapping advisor, not by this package
```

Starting a run removes the first three entries from the directory and leaves
anything else (including `decisions/`) in place, so use a new path to keep an
old export. Each decision file records the `source_started_at` of the export
it was made from, so the approval script can tell which decisions predate the
current export.

- `inventory.yaml` (schema version 3): `collection` holds filters, status,
  counts and the search-partition audit; `mapping_groups` lists request IDs
  grouped by mapping identity; `list_pages` keeps the source result rows and
  search criteria.
- `requests/<request_id>.yaml`: the EduRec identity, the student's programme
  and terms, the partner course (syllabus, credits, contact hours, assessments,
  supporting URL), the target NUS course, prerequisites, status, prior
  comments, `related_request_ids` for sibling parts of a many-to-one mapping,
  and `linked_documents`: one entry per URL with `status`, `error`, `kind`,
  `title`, `pages`, `bytes` and `path`. It is `null` unless `--scrape-urls`
  is set. `path` is relative to the run directory and set only for `fetched`
  documents.
- `documents/<hash>.txt`: the extracted text, named by a digest of the URL so
  a document shared by several requests is stored once. Text over 200 KB is a
  textbook rather than a syllabus; it is recorded as `too_large` with its size
  and title and not stored.

`request_id` is a digest of the seven identity values (student ID, career,
partner university, study program, term, mapping number, sequence). It is
stable across runs, unaffected by edits to comments or URLs, and is the only
key a decision needs to link back to a request.

`collection.status` is `complete` when every partition and page was scanned,
`row_limit_reached` for an intentional partial export, or `interrupted` when a
run stopped early. Only `complete` is an inventory.

The anonymized copy is a complete run directory, documents included. It
replaces each student ID with a `student-<hex>` pseudonym that is consistent
within the copy, drops student names and user IDs, and sets
`collection.anonymized: true`. The salt is random per run and never stored, so
pseudonyms cannot be reversed or matched across exports.

## Guarantees and limits

- Only search, row-open, list-return, next-page and view-size actions exist in
  code. No comments, decisions or saves.
- With `--scrape-urls`, URLs in the supporting URL, synopsis, other
  information, prerequisites and comments are fetched through the browser
  session. PDFs are read with pypdf, HTML and text with BeautifulSoup, and
  Dropbox links use `dl=1`. Failures are recorded per URL and never abort a run.
- Many-to-one groups stay `completeness: unverified`; sibling requests may fall
  outside the current reassignee or term scope. The NUS syllabus is not
  exported because the detail page does not show it.
- Scope is whatever the signed-in user can see in Course Mapping Approval, with
  no Mapping Status filter.
- Identifiers and displayed numbers stay strings. Session tokens and raw HTML
  are not exported. Exports contain personal data; keep `../edurec-data/` private.

## Quality gate

Run before considering any change done; all four must pass:

```sh
ruff format . && ruff check --fix . && mypy && pytest
```

The code is fully type-annotated and checked with `mypy --strict`. Records are
dataclasses in `models.py`: the request records hold what a mapping decision
needs, the collection records hold the extraction audit, and `Decision` mirrors
one `decisions/<request_id>.yaml` file as `.claude/agents/course-mapping.md` specifies it. `Document.to_dict()` and
`Request.to_dict()` produce the plain dictionaries written to YAML.

Tests parse the trimmed pages in `tests/fixtures/` and drive local headless
browsers with intercepted requests for search operators, the View 100 switch
and document fetching. They never contact EduRec.
