# edurec-mappings

Read-only extraction of NUS EduRec **Course Mapping Approval** requests into
an append-only store of anonymized YAML files. The package collects structured evidence, optionally the
text of linked syllabus documents, and never decides anything itself. Its
`review` command (below) walks a human reviewer through the advisor's proposals in
EduRec; the reviewer presses EduRec's buttons, the program only watches and records the outcome.

## Layout

```
edurec-mappings/
├── pyproject.toml            # package metadata; installs the `edurec-mappings` command
├── src/edurec_mappings/
│   ├── cli.py                # argument parsing, login prompt, browser lifecycle
│   ├── export.py             # cap-aware search loop collecting one export in memory
│   ├── browser.py            # EduRec navigation (search form, paging, detail, View 100)
│   ├── page.js               # scripts run in the EduRec frame: settle waits, review panel hook
│   ├── models.py             # typed dataclasses for requests, proposals, outcomes; YAML (de)serialisation
│   ├── parse.py              # list/detail HTML parsing into those records
│   ├── documents.py          # download and text extraction of URLs in course details
│   ├── anonymize.py          # keyed request ids and student pseudonyms
│   ├── store.py              # the append-only store: request versions, pending, identities
│   ├── review.py             # reviewer walk-through of the proposals, one outcome file each
│   ├── templates/            # Jinja2 review panel (panel.html) and its stylesheet (panel.css)
│   └── terms.yaml            # terms searched when --term is blank
└── tests/                    # unittest suite; fixtures/ holds trimmed EduRec pages

../edurec-data/               # runtime data, kept outside the package and out of Git
├── module-mappings/          # the store (below); its private/ holds personal data
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
edurec-mappings export --scrape-urls --store ../edurec-data/module-mappings
edurec-mappings pending --store ../edurec-data/module-mappings
```

The command has four subcommands, `export`, `pending`, `review` and `fetch`;
`edurec-mappings --help` lists them and `python3 -m edurec_mappings` is
equivalent. `--store` (required) names the
store for the first three. Log in through VNC, accept the
policy if you agree. `export`
runs once the approval form is visible (detected automatically after login; Enter retries at once): it applies its own search filters, switches the results grid
to **View 100** and opens every matching request, keeping them in memory;
with `--scrape-urls` it then fetches every URL
found in the course details (an HTML
page whose text is under 1,000 characters is re-read in a browser page so that
script-rendered catalogues such as Korea University, NYCU and TUMonline yield
their content rather than a loading shell; Google Drive file links and Google
Docs links are fetched through their download and export endpoints, which
serve files shared with anyone without a sign-in). Finally it adds the
requests to the store (below) and prints the export status (`complete`,
`row_limit_reached` or `interrupted`) and how many new versions were stored.
When collection stops early the requests collected so far are still stored,
except that with `--scrape-urls` a request is stored only once its documents
were fetched; the command then exits with an error.

`edurec-mappings pending` prints, one per line and relative to the store, the
file `requests/<request_id>/<hash>.yaml` of every request whose latest version
has no proposal yet, parts of a many-to-one mapping consecutively. It is the
course-mapping advisor's work queue.

| Argument | Supplied | Blank or omitted |
| --- | --- | --- |
| `--reassign-id` | Exact, case-insensitive match on the ReassignID column, filtered locally | All reassignees, including unassigned |
| `--term` | One four-digit term code, e.g. `2610` | Every term in `terms.yaml` (override with `--terms-file`) |
| `--rows` | Stop after this many unique requests | All matching requests |
| `--scrape-urls` | Fetch URLs in course details and store their text | `linked_documents` stays `null` |
| `--cdp-url URL --ready` | Attach to a running, logged-in Chromium; left open on exit | Launch a browser on `--profile` and prompt for login |

`export --help` lists the rest (`--profile`, `--proxy`, `--timeout`).

`edurec-mappings fetch URL` prints the title and text of one page after its
scripts have run, in a fresh headless browser without login (`--proxy`,
`--timeout`, `--settle` seconds after network idle). It is for the reviewer or
the course-mapping advisor to retry a link that an older export recorded as a
stub or failure.

## Why searches are subdivided

EduRec displays at most 300 rows per search and paging cannot reach the rest.
Observed on 2026-09-21: a blank-term search showed 300 rows, term `2610` showed
293, and term `2620` alone was capped. The extractor therefore searches each
configured term separately and, when a search is capped, splits it by student
ID, then mapping group and sequence. Inclusive boundaries can overlap, so
requests are deduplicated by full identity. A search that stays capped after
every split ends the export as `interrupted` rather than with a false
claim of completeness. Results follow search order, not a global sort.

## Store

The store is append-only: exports add files and nothing is ever deleted or
reset, so an interrupted export just adds fewer files. It is laid out so that
an AI advisor can read one request at a time:

```
module-mappings/
├── requests/<request_id>/<hash>.yaml    # anonymized request versions
├── proposals/<request_id>/<hash>.yaml   # written by the course-mapping advisor, not by this package
├── outcomes/<request_id>/<hash>.yaml    # written by `edurec-mappings review`
├── documents/<url_hash>.txt             # scraped text, latest per URL (--scrape-urls)
└── private/
    ├── identities.yaml                  # request_id -> real student ID; read only by review
    └── secret                           # key of request ids and pseudonyms; never regenerated
```

- `requests/<request_id>/<hash>.yaml` (schema version 7): `schema_version`,
  `created_at` (when this version was first written), `request_id`, the
  EduRec identity with the student ID replaced by a pseudonym, the student's
  programme and terms, the partner course (syllabus, credits, contact hours,
  assessments, supporting URL), the target NUS course, prerequisites, status,
  prior comments, `related_request_ids` for sibling parts of a many-to-one
  mapping, and `linked_documents`: one entry per URL with `status`, `error`,
  `kind`, `title`, `pages`, `bytes` and `path`. It is `null` unless
  `--scrape-urls` is set. `path` is relative to the store and set only for
  `fetched` documents.
- `<hash>` is 16 hex digits of a digest of the request's content, including
  the text of each linked document, its comments and `related_request_ids`,
  but not `created_at`, `schema_version`, the EduRec status or fetch metadata
  (status, error, size and so on). An export writes a request only when the
  digest differs from its latest stored version: an unchanged request is not
  rewritten and a changed one gets a new file beside the old one. The latest
  version of a request is the one with the latest `created_at`; a request that
  returns to earlier content rewrites that earlier file with a new
  `created_at`, which makes it the latest again.
- Proposals and outcomes are named by the request version they were made on, so
  a re-export can neither orphan nor misapply them: a request whose content
  changed is a new version without a proposal and is pending again. The path
  is their only key; neither file holds a `request_id`.
- `documents/<url_hash>.txt`: the extracted text, named by a digest of the URL
  so a document shared by several requests is stored once, and overwritten
  with the latest text on each export. Text over 200 KB is a textbook rather
  than a syllabus; it is recorded as `too_large` with its size and title and
  not stored.
- `private/secret` holds 32 random bytes (as hex), created by the first export.
  Both identifiers are keyed with it: `request_id` is the first 24 hex digits
  of HMAC-SHA256 over the seven identity values (student ID, career, partner
  university, study program, term, mapping number, sequence) in canonical
  JSON, and the pseudonym is `student-` followed by the first 12 hex digits of
  HMAC-SHA256 over the student ID. Both are stable across exports and
  unaffected by edits to comments or URLs, yet cannot be reversed by trying
  student IDs without the secret. A new secret would change every identifier
  and detach every proposal, so back it up with the store.
- `private/identities.yaml` maps each `request_id` to the real student ID and
  is merged on every export; only `review` reads it, to search EduRec. Nothing
  under `requests/`, `proposals/` or `outcomes/` holds a real student ID; do not
  give the advisor access to `private/`.

The export status is `complete` when every partition and page was scanned,
`row_limit_reached` for an intentional partial export, or `interrupted` when
it stopped early. Only `complete` covers every matching request; the status is
printed, not stored.

## Review

## Review

Once the advisor has written proposals into the store, submit
them in EduRec yourself with the program as a guide. Try `--dry-run` first: it
walks the same queue with the action buttons disabled, so you can read the
panels and check the pre-filled comments without submitting or storing anything.

```sh
edurec-mappings review --store ../edurec-data/module-mappings --dry-run
edurec-mappings review --store ../edurec-data/module-mappings
```

The queue is the latest version of every request that has a proposal for that
version and no outcome for it, parts of a many-to-one mapping consecutively.
`--request-id` and `--verdict` (repeatable) narrow it; the browser options are
the same as for extraction. A malformed proposal file stops the review with an
error naming it.

For each queued request the program searches EduRec by the request's identity
(the real student ID comes from `private/identities.yaml`),
opens the detail, checks that it is still `Pending Approval` and still shows
the exported courses, mapping number and sequence, pre-fills the comment box
with the proposal's comment on top of any existing comment (EduRec replaces
the field, so the earlier text is kept below it), and injects a panel on the
right. The panel is rendered in a shadow root so EduRec's styles cannot leak
into it. From top to bottom it shows:

- a header with two coloured badges: the selected verdict (green for approve,
  red for reject, amber for the two requests; the matching EduRec button gets
  an outline in the same colour) and the overlap percentage (green from 70%,
  amber from 40%, red below); under them the course (PU subject and number,
  university, NUS course, taken from the request), a thin progress bar and
  the caption `<position> of <total> this session · <submitted> of <requests>
  overall`, where `<submitted>` counts the requests whose latest version was
  reviewed with a verdict and `<requests>` the requests in the store;
- two tabs, **Recommended** and **Fallback**, each a bordered card. Clicking a
  tab only previews its pane. A selectable pane opens with **Decision:**
  and the verdict (the Recommended pane adds the confidence badge: green high,
  amber medium, red low), then the fallback's rationale where it applies, the
  comment text and a tools line with **Reset comment**, which re-fills the box
  with the selected tab's text (on top of the earlier comment), and a
  **Select** button that reads **Selected** and is disabled on the selected
  tab (Recommended at first); a "modified" marker appears on the selected
  pane while the box differs from its text. Without a fallback verdict the
  Fallback pane reads "No fallback" with the advisor's reason and cannot be
  selected. Pressing Select puts that tab's text in the box, moves the
  button outline to its verdict and updates the header pill, which follows
  the selection, not the tab being viewed; if you had edited the box it asks
  first;
- previous comments already on the request;
- the remap section (**Target:** and the analysis), when the advisor proposed one;
- concerns, when there are any;
- overlap, missing and extra topics as three lists, then every sibling part of
  a many-to-one mapping by its course with the verdict submitted on its latest
  version, flagged when it differs from this proposal's verdict;
- pinned at the bottom, a **Skip** button with an optional reason field.

You then edit the comment if you wish and press one of EduRec's own buttons,
or the panel's Skip. Pressing a button other than the selected tab's verdict
asks for confirmation; if the button matches the fallback while Recommended
is selected, the question offers to select the fallback and submit its comment.
Cancel counts as a skip. The program never presses an action button. After
your click it waits for the postback, re-reads the status and moves on. A
request that no longer appears in the approval queue (Request Remapping and
Request More Information remove it) counts as verified with status `not in
approval queue`; if the status is still `Pending Approval` the
session stops rather than guessing. A comment that is empty or still contains
`[` or `XXXX` is skipped without being entered.

PeopleSoft re-renders the page on many harmless interactions (collapsing a
section, sorting a grid, tabbing out of a changed field), which removes the
panel and the click hook. The program notices, re-installs them with the
comment box left as you had it, the same selected and viewed tabs, scroll
position and skip reason,
and keeps waiting; the dry-run buttons are disabled again too. If the detail page goes away without a recognised button
(you navigated elsewhere), the request is skipped with reason
"reviewer left the page". A request that already left the queue before it was
opened is closed with an outcome (below) and the session continues.

Only clicks made while the panel is shown are recorded. When the session stops
on an error it closes the browser instead of leaving the page open, because a
click on an unwatched page would go unrecorded; do not act in EduRec after the
program has stopped.

Every outcome is printed. A submitted verdict is written to
`outcomes/<request_id>/<hash>.yaml` before the next request opens: `action`
(the verdict submitted), `comment` (as submitted), `recorded_at`, and a
`reason` when the submission could not be verified.
The file is written provisionally the moment the click is seen, so an error
before verification still leaves the verdict on record. A request that already
left the approval queue gets an outcome with action `not in approval queue`,
which closes it. Other skips (a reason typed into the
panel reads `skipped by the reviewer: <reason>`) and everything in a dry run
write nothing, so those requests are offered again next session. A version with
an outcome is never offered again; a later export that changes the request adds a
new version, which needs a new proposal. The panel warns when a sibling was
already submitted with a different action.

## Guarantees and limits

- Extraction only uses search, row-open, list-return, next-page and view-size
  actions. `review` additionally sets the comment box and waits for a click; it
  never triggers an EduRec action button itself.
- With `--scrape-urls`, URLs in the supporting URL, synopsis, other
  information, prerequisites and comments are fetched through the browser
  session. PDFs are read with pypdf, HTML and text with BeautifulSoup, and
  Dropbox links use `dl=1`. Failures are recorded per URL and never abort a run.
- A many-to-one request's `related_request_ids` lists only the siblings the
  same export collected; others may fall outside the current reassignee or term scope. The NUS syllabus is not
  exported because the detail page does not show it.
- Scope is whatever the signed-in user can see in Course Mapping Approval, with
  no Mapping Status filter.
- Identifiers and displayed numbers stay strings. Session tokens and raw HTML
  are not exported. `private/` holds real student IDs and the key that protects
  the pseudonyms; keep `../edurec-data/` private.

## Quality gate

Run before considering any change done; all four must pass:

```sh
ruff format . && ruff check --fix . && mypy && pytest
```

The code, tests included, is fully type-annotated (ruff's `ANN` rules) and
checked with `mypy --strict`. Records are
dataclasses in `models.py`: the request records hold what a mapping decision
needs, `Export` holds one export in memory, `Proposal` mirrors one
`proposals/<request_id>/<hash>.yaml` file as `.claude/agents/course-mapping.md`
specifies it, and `Outcome` one outcome file. `plain` turns a record into the
dictionary written to YAML and `hydrate` reads one back. Both go through a
pydantic `TypeAdapter`; loading is strict, as for JSON: no type coercion, no
unknown keys, and no YAML-only values such as unquoted timestamps. A malformed
proposal file stops the review with an error naming the file. Old data is
migrated when the schema changes, not accepted by the loader.

Tests parse the trimmed pages in `tests/fixtures/` and drive local headless
browsers with intercepted requests for search operators, the View 100 switch
and document fetching. They never contact EduRec.
