## What this changes

<!-- One or two sentences. What does a user get, or what breaks less? -->

## Why

<!-- The problem behind the change. Link an issue if there is one. -->

## Checklist

Tick only what you actually did — an optimistic tick is a false claim about the
state of the repository. Leave the rest unticked with a note saying why.

- [ ] `docs/functions.md` was read before adding functions, and new, renamed or
      removed public functions have their rows updated
- [ ] `CHANGELOG.md` has an entry under `[Unreleased]` (skip for
      repository plumbing — CI config, templates, editor settings)
- [ ] Documentation under `docs/` updated alongside the code
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `python -m pytest` passes
- [ ] `docker build .` succeeds

## Testing

<!-- What you actually ran, and what you did not. If a path was only reasoned
     about rather than exercised, say so here — that is the useful half. -->
