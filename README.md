# edurec-mappings

Read-only extraction of NUS EduRec **Course Mapping Approval** requests into an
append-only store of pseudonymized YAML files, plus a guided `review` session for
submitting proposals in EduRec.

It is specific to the NUS EduRec (PeopleSoft) component and needs an account
with access to Course Mapping Approval. Proposals are written by a human
reviewer or an agent, one per request version; this package collects the
requests and submits the proposals, it does not make them.

## Layout

```
edurec-mappings/
├── src/edurec_mappings/
│   ├── cli.py              # subcommands, login prompt, browser lifecycle
│   ├── export.py           # cap-aware search loop and its subdivision
│   ├── edurec.py           # EduRec navigation and the review page
│   ├── parse.py            # list and detail HTML into records
│   ├── documents.py        # fetching and text extraction of linked URLs
│   ├── pseudonymize.py     # keyed request ids and student pseudonyms
│   ├── store.py            # versions, proposals, outcomes, student IDs
│   ├── review.py           # guided review
│   ├── models.py           # dataclasses and strict YAML I/O
│   ├── page.js             # scripts run in the EduRec frame
│   ├── templates/          # review panel
│   └── terms.yaml          # terms searched when --term is blank
└── tests/
```

Runtime data lives in `../data/`, outside the repository: the store in
`course-mappings/` and the EduRec login in `browser-profile/`.

## Setup

The devcontainer (`.devcontainer/`) provides Python 3.14, Chromium and a desktop,
installs the package in editable mode, and keeps `../data/` in the
`edurec-mappings-data` Docker volume across rebuilds. Rebuild it after changing
dependencies. Without it, on Python 3.14 or later:

```sh
pip install -e '.[dev]'
python3 -m playwright install chromium
```

In the devcontainer, log in through the desktop on forwarded port **6080**
(password `vscode`; keep the port private). Commands open Chromium on that
desktop. EduRec must be accessed from the NUS network.

## Commands

Run from this directory; default paths are relative to it.

```sh
edurec-mappings export --documents --store ../data/course-mappings
edurec-mappings pending --store ../data/course-mappings
edurec-mappings review --store ../data/course-mappings --dry-run
edurec-mappings validate PROPOSAL...
edurec-mappings fetch URL
edurec-mappings render URL
```

`--cdp-url` attaches `export` or `review` to a running, logged-in Chromium and
leaves it open on exit; `--skip-login` skips the login prompt. See `--help` for the
rest.

### export

Waits for the approval form after login (Enter retries), searches, switches the
grid to **View 100**, opens every matching request and adds the pseudonymized
requests to the store. It prints the status (`complete`, `limit_reached`
or `interrupted`) and the number of new versions. An interrupted export exits
with an error but stores what it collected; with `--documents` documents are
fetched after collection, so an interruption during collection stores nothing.

| Option | Given | Blank or omitted |
| --- | --- | --- |
| `--reassigned-to` | Exact, case-insensitive ReassignID | All, including unassigned |
| `--term` | One four-digit term code, e.g. `2610` | Every term in `terms.yaml` (or `--terms-file`) |
| `--limit` | Stop after this many unique requests | All |
| `--documents` | Fetch every URL in the course details | Documents already in the store are referenced |

**Search subdivision.** EduRec shows at most 300 rows per search and paging
cannot go further. Each term is searched separately; a capped search is split
by student ID, then mapping group and sequence. A search still capped after
every split ends the export as `interrupted`.

**Documents.** URLs in the partner course title, supporting URL, synopsis,
other information, prerequisites and comments are fetched with the browser's
user agent and proxy but without its cookies, so a link to a page that needs the
EduRec account's sign-in fails instead of being stored. Dropbox, Google Drive file and Google Docs share links are rewritten to
their download or export URLs. PDF, Word (`.docx`), HTML and plain text are
read; a zip archive (such as a Dropbox folder link) and the top level of a
shared Google Drive folder (up to 20 files, no subfolders) are read file by
file into one text with a `=== name ===` heading per file. An HTML page with
under 1,000 characters of text, or a `#/` route, is re-read with `render --html`
to catch script-rendered catalogues; that browser runs in its own process, is not
signed in, and is killed 30 s after its page timeout and settle time. A short page
titled as a sign-in or bot check is recorded as `failed`. HTTP 429 and 502–504
are retried once after 10 s. Text over 200 KB is recorded as `too_large` and not
stored. Failures are recorded per URL and never abort the export.

### pending

Prints the store-relative `requests/<request_id>/<hash>.yaml` of the latest
version of every request without a proposal, listing sibling parts of a
many-to-one mapping together.

### review

Walks the latest versions that have a proposal and no outcome, listing siblings
together; `--request-id` and `--verdict` (repeatable, e.g. `request_remapping`)
narrow the queue.
`--dry-run` shows the same panels with the action buttons disabled and writes
nothing.

For each request it searches EduRec by the real student ID, pre-fills the
comment box with the proposal's comment above any existing text, and injects a
panel with the proposal's recommended and fallback verdicts, evidence and
siblings. **Select** loads a verdict's comment into the box and marks its
EduRec button; the program never presses it. **Skip** moves on. A request
whose live detail differs from the export, or whose proposal comment is empty
or still has a placeholder (`[`, `XXXX`), is skipped.

A button other than the selected verdict asks for confirmation (Cancel counts
as a skip). After the postback the status is re-read; a request that left the
approval queue counts as verified, one still `Pending Approval` stops the
session. Leaving the detail page skips the request. Only clicks made while the
panel is shown are recorded; on error the browser is closed, so do not act in
EduRec after the program stops.

A submitted verdict is written to `outcomes/<request_id>/<hash>.yaml` with
`verified: false` when the click is seen, and rewritten with `verified: true`
once the request has left the queue; a request that had already left gets
`verdict: null`. Skips are offered again next session.

### validate

Checks proposal files as `review` loads them, including its comment checks.
Prints `ok` or one error line per bad file.

### fetch

Reads one URL as `export --documents` does, without storing it, and prints the
result's status, kind, title, URL and error, then its text. For retrying links an
export recorded as `empty` or `failed`. The re-render takes `--timeout` and
`--settle`; `--all-files` reads a whole Drive folder, subfolders included.

### render

Prints the title and text of one page after its scripts have run, in a fresh
headless browser without login. `--html` writes the rendered HTML instead.

## Store

Exports only add or rewrite files; nothing is deleted.

The store is personal data. Request files carry a pseudonym instead of the
student ID, but `private/` maps pseudonyms back to student IDs, and comments and
linked documents are stored as written. Keep the store out of version control
and shared folders.

```
course-mappings/
├── requests/<request_id>/<hash>.yaml    # pseudonymized request versions (export)
├── proposals/<request_id>/<hash>.yaml   # proposals (by a reviewer or an agent)
├── outcomes/<request_id>/<hash>.yaml    # submitted verdicts (review)
├── documents/<url_hash>/<hash>.md       # fetch results per URL: front matter + text
└── private/                             # keep private
    ├── student_ids.yaml                 # request_id -> real student ID; read only by review
    └── hmac_key                         # back it up
```

- **Request file**: the fields are defined by `models.Request`; a proposal's by
  `models.Proposal`.
  `sibling_request_ids` lists only siblings found in the same export.
- **Document file**: the fields of `models.LinkedDocument` as front matter and
  the text as the body. `status` is `fetched`, `empty` (no text found),
  `too_large` or `failed`; only `fetched` keeps the text. A result that repeats
  the newest one is not written again; a readable result is compared with the
  newest readable one.
- **`documents`** in a request file gives each URL's newest readable result
  (`path`, `null` if none). It comes from the store, not the export, so an
  export without `--documents` or a failed fetch leaves it unchanged.
- **`<hash>`** covers the request content, including those document
  references, comments and siblings, but not EduRec status. An export
  writes a file only when the hash differs from the latest version (the one
  with the newest `created_at`); returning to earlier content rewrites that
  file with a new `created_at`.
- **Proposals and outcomes** are keyed by path, so they bind to one request
  version: changed content becomes a new version, pending again.
- **Identifiers**: `request_id` and the `student-<hex>` pseudonym are HMAC digests
  keyed by `private/hmac_key`, stable across exports and irreversible without it.
  A new key would detach every proposal.

## Scope

- Export only searches, opens rows, returns to the list, pages and switches the
  view size. Review also fills the comment box and watches for clicks.
- Scope is whatever the signed-in account sees in Course Mapping Approval, with no
  Mapping Status filter. The NUS syllabus is not on the detail page and is not
  exported.
- Session tokens and raw HTML are not stored.
- Loading is strict; a malformed proposal stops `review` naming the file.

## Quality gate

```sh
ruff format . && ruff check --fix . && mypy && pytest
```

Tests use `tests/fixtures/` and local headless Chromium with intercepted
requests; they never contact EduRec.

## License

MIT; see `LICENSE`.
