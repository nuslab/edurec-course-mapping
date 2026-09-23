# edurec-mappings

Read-only extraction of NUS EduRec **Course Mapping Approval** requests into
a run directory of YAML files. The package collects structured evidence, optionally the
text of linked syllabus documents, and never decides anything itself. Its
`review` command (below) walks a human reviewer through the advisor's decisions in
EduRec; the reviewer presses EduRec's buttons, the program only watches and logs.

## Layout

```
edurec-mappings/
├── pyproject.toml            # package metadata; installs the `edurec-mappings` command
├── src/edurec_mappings/
│   ├── cli.py                # argument parsing, login prompt, browser lifecycle
│   ├── export.py             # cap-aware search loop with YAML checkpoints
│   ├── browser.py            # EduRec navigation (search form, paging, detail, View 100)
│   ├── page.js               # scripts run in the EduRec frame: settle waits, review panel hook
│   ├── models.py             # typed dataclasses for the export and decisions; YAML (de)serialisation
│   ├── parse.py              # list/detail HTML parsing into those records
│   ├── documents.py          # download and text extraction of URLs in course details
│   ├── anonymize.py          # pseudonymised copy of an export
│   ├── review.py             # reviewer walk-through of the decisions, with reviewed.yaml log
│   ├── templates/            # Jinja2 review panel (panel.html) and its stylesheet (panel.css)
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
edurec-mappings export --scrape-urls --anonymize --run ../edurec-data/output/module-mappings
```

The command has three subcommands, `export`, `review` and `fetch`;
`edurec-mappings --help` lists them and `python3 -m edurec_mappings` is
equivalent. Log in through VNC, accept the
policy if you agree. `export`
runs three steps once the approval form is visible (detected automatically after login; Enter retries at once): it applies its own search filters, switches the results grid
to **View 100**, opens every matching request and checkpoints the run
directory after each detail; with `--scrape-urls` it then fetches every URL
found in the course details and stores their text under `documents/` (an HTML
page whose text is under 1,000 characters is re-read in a browser page so that
script-rendered catalogues such as Korea University, NYCU and TUMonline yield
their content rather than a loading shell; Google Drive file links and Google
Docs links are fetched through their download and export endpoints, which
serve files shared with anyone without a sign-in); with
`--anonymize` it finally writes a pseudonymised copy of the run next to it.

| Argument | Supplied | Blank or omitted |
| --- | --- | --- |
| `--reassign-id` | Exact, case-insensitive match on the ReassignID column, filtered locally | All reassignees, including unassigned |
| `--term` | One four-digit term code, e.g. `2610` | Every term in `terms.yaml` (override with `--terms-file`) |
| `--rows` | Stop after this many unique requests | All matching requests |
| `--scrape-urls` | Fetch URLs in course details and store their text | `linked_documents` stays `null` |
| `--anonymize` | Also write `<run>-anonymized/` (or `--anonymized-run`) | Original only |
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
every split ends the run with an `interrupted` checkpoint rather than a false
claim of completeness. Results follow search order, not a global sort.

## Export

`--run` names a run directory that is both the export and the checkpoint.
It is laid out so that an AI advisor can read one request at a time instead of
the whole inventory:

```
module-mappings/
├── inventory.yaml            # collection audit, mapping_groups, list_pages
├── requests/<request_id>.yaml   # one file per unique request
├── documents/<hash>.txt      # scraped text, one file per URL (--scrape-urls)
├── decisions/<request_id>.yaml  # written by the course-mapping advisor, not by this package
└── decisions/reviewed.yaml    # written by `edurec-mappings review`
```

Starting a run removes the first three entries from the directory and leaves
anything else (including `decisions/`) in place, so use a new path to keep an
old export. Each decision file records the `source_started_at` of the export
it was made from, so the approval script can tell which decisions predate the
current export.

- `inventory.yaml` (schema version 4): `collection` holds filters, status,
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

## Review

Once the advisor has written `decisions/` inside the anonymized copy, submit
them in EduRec yourself with the program as a guide. Try `--dry-run` first: it
walks the same queue with the action buttons disabled, so you can read the
panels and check the pre-filled comments without submitting anything.

```sh
edurec-mappings review --run ../edurec-data/output/module-mappings --dry-run
edurec-mappings review --run ../edurec-data/output/module-mappings
```

`--decisions` defaults to `<run>-anonymized/decisions`; `--request-id` and
`--verdict` (repeatable) narrow the queue; the browser options are the same as
for extraction. Decisions made from another export (their `source_started_at`
differs from the inventory) or without a request file are reported and never
offered.

For each queued request the program searches EduRec by the request's identity,
opens the detail, checks that it is still `Pending Approval` and still shows
the exported courses, mapping number and sequence, pre-fills the comment box
with the decision's comment on top of any existing comment (EduRec replaces
the field, so the earlier text is kept below it), and injects a panel on the
right. The panel is rendered in a shadow root so EduRec's styles cannot leak
into it. From top to bottom it shows:

- a header with two coloured badges: the selected verdict (green for approve,
  red for reject, amber for the two requests; the matching EduRec button gets
  an outline in the same colour) and the overlap percentage (green from 70%,
  amber from 40%, red below); under them the course, a thin progress bar and
  the caption `<position> of <total> this session · <submitted> of <requests>
  overall`;
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
  a many-to-one mapping by its course with its logged outcome, flagged when it
  was submitted with a different action;
- pinned at the bottom, a **Skip** button with an optional reason field.

You then edit the comment if you wish and press one of EduRec's own buttons,
or the panel's Skip. Pressing a button other than the selected tab's verdict
asks for confirmation; if the button matches the fallback while Recommended
is selected, the question offers to select the fallback and submit its comment.
Cancel counts as a skip. The program never presses an action button. After
your click it waits for the postback, re-reads the status and moves on. A
request that no longer appears in the approval queue (Request Remapping and
Request More Information remove it) is logged with status `not in approval
queue` and counts as verified; if the status is still `Pending Approval` the
session stops rather than guessing. A comment that is empty or still contains
`[` or `XXXX` is skipped without being entered.

PeopleSoft re-renders the page on many harmless interactions (collapsing a
section, sorting a grid, tabbing out of a changed field), which removes the
panel and the click hook. The program notices, re-installs them with the
comment box left as you had it, the same selected and viewed tabs, scroll
position and skip reason,
and keeps waiting; the dry-run buttons are disabled again too. If the detail page goes away without a recognised button
(you navigated elsewhere), the request is logged as a skip with reason
"reviewer left the page". A request that already left the queue before it was
opened is logged as a skip and the session continues.

Only clicks made while the panel is shown are logged. When the session stops
on an error it closes the browser instead of leaving the page open, because a
click on an unwatched page would go unrecorded; do not act in EduRec after the
program has stopped.

Every outcome is appended to `decisions/reviewed.yaml` before the next request
opens: request id, recommended verdict, the action taken (a verdict or
`skip`), the comment as submitted, which text it was (`comment_source` is
`recommended`, `fallback`, or `edited` when the box differed from the selected
tab's text; `comment_edited` says the same as a flag), status before and
after, timestamp, dry-run flag and a reason for skips or unverified
submissions. A skip reason typed into the panel is logged as
`skipped by the reviewer: <reason>`. Requests logged with a verdict are never offered again, including
unverified ones; skipped requests are offered on the next session. Parts of a
many-to-one mapping are queued consecutively and the panel warns when a
sibling was already submitted with a different action.

## Guarantees and limits

- Extraction only uses search, row-open, list-return, next-page and view-size
  actions. `review` additionally sets the comment box and waits for a click; it
  never triggers an EduRec action button itself.
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

The code, tests included, is fully type-annotated (ruff's `ANN` rules) and
checked with `mypy --strict`. Records are
dataclasses in `models.py`: the request records hold what a mapping decision
needs, the collection records hold the extraction audit, and `Decision` mirrors
one `decisions/<request_id>.yaml` file as `.claude/agents/course-mapping.md` specifies it. `plain`
turns a record into the dictionary written to YAML (`Document.inventory()` is the
export without its requests) and `hydrate` reads one back. Both go through a
pydantic `TypeAdapter`; loading is strict, as for JSON: no type coercion, no
unknown keys, and no YAML-only values such as unquoted timestamps. A malformed
decision file stops the review with an error naming the file. Old data is
migrated when the schema changes, not accepted by the loader.

Tests parse the trimmed pages in `tests/fixtures/` and drive local headless
browsers with intercepted requests for search operators, the View 100 switch
and document fetching. They never contact EduRec.
