# Contributing

## Ground rule: check the function index first

Before writing a new function, read [`docs/functions.md`](docs/functions.md). If
something there already does the job, call it. Writing a second implementation
of an existing thing is not "adding code" — it creates the bug that fires the
next time someone fixes only one copy.

If a second implementation is genuinely necessary, say so in its row and in a
comment on the function itself:

```python
# Deliberately not `existing_thing`, because <reason>. Do not add a third.
```

A duplicate with no such note is an accident by definition, and the next reader
cannot tell which it was.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run it locally without Docker:

```bash
export XCP_PULSE_DATA_DIR=./data
export XCP_PULSE_ADMIN_PASSWORD_HASH="$(echo 'devpassword' | .venv/bin/python -m app.hashpw | grep -oP '(?<=HASH=).*')"
.venv/bin/uvicorn app.main:app --reload --port 8080
```

## Before opening a pull request

The same four things CI runs:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest
docker build -t xcp-pulse:dev .
```

## Conventions

**Update the function index in the same commit.** A commit that adds, renames,
changes the signature of, or removes a public function and does not touch
`docs/functions.md` is incomplete. `tests/test_function_index.py` fails the
build when a function has no row, or a row names a function that is gone. It
does not check the prose columns — those are the reviewer's job.

**Update the CHANGELOG in the same pass.** Every user-facing change gets an entry
under `## [Unreleased]`, in the right subsection, newest first. Repository
plumbing — CI config, issue templates, editor settings — gets no entry: a
changelog is read by someone deciding whether to upgrade, and housekeeping
entries bury the real ones.

**Keep entries proportionate.** Say what changed and why it matters, then stop.
Look at the neighbouring entries for the right length.

**Documentation ships with the code**, not after it. A stage that adds a feature
adds or updates its docs page in the same branch.

**One place for each setting.** `app/config.py` is the only module that reads
`os.environ`.

**Migrations are append-only.** Add to the end of `_MIGRATIONS` in `app/db.py`.
Editing an applied migration leaves existing databases behind.

**Never commit a real log bundle.** Bundles collected from a live pool contain
internal addresses, usernames and session tokens. Test fixtures use synthetic
files with planted fake secrets.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
`docs:`, `test:`, `refactor:`, `chore:`.

## Branches and releases

Work happens on `dev` and reaches `main` through a pull request. The changelog
version section is part of the branch the PR carries. The tag is created on the
merge commit on `main` **after** the merge — a tag made on `dev` points at a
commit the merge replaced.
