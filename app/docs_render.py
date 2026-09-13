"""Renders this project's own markdown into the in-app User manual (/help).

This is a **user manual**, not a mirror of the whole GitHub docs set: a
**User guide** group of pages covering using XCP Pulse once the container is
running — one page per feature area, under ``docs/user-guide/`` (new content
written only for this section; none of it is linked from README's own docs
table, and none of it carries a "back to the README" line, because none of
it has a GitHub page to return to) — plus Installation, Configuration and
Architecture as top-level pages, the three GitHub docs still useful to a
user rather than a contributor. ``README.md``, ``docs/functions.md`` (a
contributor reference) and ``docs/roadmap.md`` (the roadmap process itself)
are deliberately not rendered here — GitHub remains the place to read those.

Docs are read and rendered once per process, at first request, and cached in
memory: they ship inside the image, so they cannot change without a restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import markdown

REPO_ROOT = Path(__file__).parent.parent
_USER_GUIDE_DIR = REPO_ROOT / "docs" / "user-guide"

# (slug, title, path, group). ``group`` is the sidebar group a page nests
# under — "User guide" pages render as a collapsible sub-list; ``None`` is a
# flat top-level entry. Order here is both file-list and display order.
# README.md, docs/functions.md and docs/roadmap.md are intentionally not
# listed — see the module docstring.
PAGES: list[tuple[str, str, Path, str | None]] = [
    ("first-login", "First login", _USER_GUIDE_DIR / "first-login.md", "User guide"),
    ("dashboard", "Dashboard", _USER_GUIDE_DIR / "dashboard.md", "User guide"),
    ("collect", "Collect", _USER_GUIDE_DIR / "collect.md", "User guide"),
    ("redaction", "Redaction", _USER_GUIDE_DIR / "redaction.md", "User guide"),
    ("jobs", "Jobs", _USER_GUIDE_DIR / "jobs.md", "User guide"),
    ("findings", "Findings", _USER_GUIDE_DIR / "findings.md", "User guide"),
    ("support-package", "Support package", _USER_GUIDE_DIR / "support-package.md", "User guide"),
    ("date-ranges", "Date ranges", _USER_GUIDE_DIR / "date-ranges.md", "User guide"),
    ("settings", "Settings", _USER_GUIDE_DIR / "settings.md", "User guide"),
    (
        "users-and-roles",
        "Users and roles",
        _USER_GUIDE_DIR / "users-and-roles.md",
        "User guide",
    ),
    ("installation", "Installation", REPO_ROOT / "docs" / "installation.md", None),
    ("configuration", "Configuration", REPO_ROOT / "docs" / "configuration.md", None),
    ("architecture", "Architecture", REPO_ROOT / "docs" / "architecture.md", None),
]

# slug -> path, for rewriting links between docs pages.
_SLUG_BY_FILENAME = {path.name: slug for slug, _title, path, _group in PAGES}

GITHUB_BLOB_ROOT = "https://github.com/acebmxer/xcp_pulse/blob/main/"

_CALLOUT_RE = re.compile(r"^>\s*\[!(NOTE|WARNING|TIP|IMPORTANT|CAUTION)\]\s*$", re.MULTILINE)


@dataclass(frozen=True)
class DocPage:
    slug: str
    title: str
    html: str
    text: str  # plain text, for search snippets
    group: str | None  # sidebar group ("User guide"), or None for top-level


_cache: dict[str, DocPage] | None = None


def _extract_callout_blocks(text: str) -> str:
    """Turn GFM ``> [!NOTE]`` blockquotes into a div Python-Markdown leaves alone.

    GitHub's callout syntax is a blockquote whose first line is ``[!NOTE]``
    (or WARNING/TIP/IMPORTANT/CAUTION) — GitHub-specific, so plain Markdown
    renders it as an ordinary, slightly odd-looking blockquote. Lifting the
    marked lines out into a fenced HTML block before conversion, and stamping
    a class on it, is what lets the stylesheet render it as the callout it
    was meant to be instead of a quote.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _CALLOUT_RE.match(lines[i])
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        kind = match.group(1).lower()
        body_lines: list[str] = []
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            body_lines.append(re.sub(r"^>\s?", "", lines[i]))
            i += 1
        # Markdown extensions do not descend into raw HTML blocks, so the
        # body has to be converted to HTML here rather than left as markdown.
        body_html = markdown.markdown(
            "\n".join(body_lines), extensions=["fenced_code", "sane_lists"]
        )
        out.append(f'<div class="callout callout-{kind}">{body_html}</div>')
    return "\n".join(out)


def _rewrite_links(html: str, source_dir: Path) -> str:
    """Point links between docs at UI routes instead of relative .md paths.

    ``source_dir`` is the directory the source file lives in (``docs/`` or
    the repo root), which a relative link like ``roadmap.md`` or
    ``../README.md`` is relative to — resolving against the wrong directory
    is how a same-directory link like configuration.md's to roadmap.md
    previously came out missing its ``docs/`` prefix.

    A link to a file this module renders (``installation.md``,
    ``../README.md``, with or without a ``#anchor``) becomes ``/help/<slug>``.
    A link to a repo file it does not render — CHANGELOG.md, CONTRIBUTING.md,
    SECURITY.md, docs/roadmap.md — is repo-relative on GitHub itself and
    would 404 served from a UI route, so it is rewritten to an absolute
    GitHub URL instead. An already-absolute link (``https://…``) is left
    untouched.
    """

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://")):
            return match.group(0)
        path_part, _, anchor = href.partition("#")
        filename = Path(path_part).name
        slug = _SLUG_BY_FILENAME.get(filename)
        if slug is not None:
            target = f"/help/{slug}"
        else:
            absolute = (source_dir / path_part).resolve()
            repo_path = absolute.relative_to(REPO_ROOT).as_posix()
            target = GITHUB_BLOB_ROOT + repo_path
        if anchor:
            target = f"{target}#{anchor}"
        attrs = f'href="{target}"'
        if target.startswith(GITHUB_BLOB_ROOT):
            attrs += ' target="_blank" rel="noopener"'
        return attrs

    return re.sub(r'href="([^"]+\.md(?:#[^"]*)?)"', replace, html)


def _strip_readme_nav(text: str) -> str:
    """Drop the "[← back to the README]" line each mirrored docs page opens with.

    That line exists for GitHub, where docs/ pages have no other way back to
    the project root. Inside the app it is replaced by the sidebar, and
    showing both would be a dead link sitting above a working one. A no-op on
    ``user-guide.md``, which has no such line — it has no GitHub page to
    return to.
    """
    return re.sub(r"\n\[← back to the README\]\([^)]*\)\n+", "\n", text, count=1)


def _render_page(slug: str, title: str, path: Path, group: str | None) -> DocPage:
    text = path.read_text(encoding="utf-8")
    text = _strip_readme_nav(text)
    text = _extract_callout_blocks(text)

    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists", "toc"],
        extension_configs={"toc": {"permalink": False}},
    )
    html = md.convert(text)
    html = _rewrite_links(html, path.parent)

    plain_text = re.sub(r"<[^>]+>", " ", html)
    return DocPage(slug=slug, title=title, html=html, text=plain_text, group=group)


def _load_all() -> dict[str, DocPage]:
    global _cache
    if _cache is None:
        _cache = {
            slug: _render_page(slug, title, path, group) for slug, title, path, group in PAGES
        }
    return _cache


def all_pages() -> list[DocPage]:
    """All doc pages, in sidebar display order."""
    cache = _load_all()
    return [cache[slug] for slug, _title, _path, _group in PAGES]


def sidebar_groups() -> list[tuple[str | None, list[DocPage]]]:
    """Pages grouped for the sidebar: ``(group_name_or_None, pages)``, in order.

    A run of consecutive pages sharing a group becomes one collapsible
    entry; a ``None`` group is a flat top-level link. Grouping by adjacency
    rather than collecting all pages per name keeps this working if a later
    group is ever added without needing pages to also carry a sort key.
    """
    pages = all_pages()
    groups: list[tuple[str | None, list[DocPage]]] = []
    for page in pages:
        if groups and groups[-1][0] == page.group:
            groups[-1][1].append(page)
        else:
            groups.append((page.group, [page]))
    return groups


def get_page(slug: str) -> DocPage | None:
    return _load_all().get(slug)


def search(query: str, *, max_results: int = 20) -> list[tuple[DocPage, str]]:
    """Pages whose text contains ``query``, each with a short snippet.

    Case-insensitive substring search — the doc set is a handful of files
    totalling under 2,000 lines, well within what a plain scan handles
    instantly, so there is no need for an index.
    """
    query = query.strip()
    if not query:
        return []
    needle = query.lower()
    results: list[tuple[DocPage, str]] = []
    for page in all_pages():
        haystack = page.text.lower()
        pos = haystack.find(needle)
        if pos == -1:
            continue
        start = max(0, pos - 60)
        end = min(len(page.text), pos + len(query) + 60)
        snippet = page.text[start:end].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        results.append((page, snippet))
        if len(results) >= max_results:
            break
    return results
