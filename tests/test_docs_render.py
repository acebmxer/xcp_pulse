"""Rendering docs into the in-app User manual (/help).

These exercise the real files in the repo rather than fixtures, since the
whole point of this module is that it renders exactly what ships — a fixture
could drift from the real docs and hide a rewrite bug the real files trigger.
"""

from __future__ import annotations

from pathlib import Path

from app.docs_render import GITHUB_BLOB_ROOT, _rewrite_links, all_pages, get_page, search

USER_GUIDE_SLUGS = {
    "first-login",
    "dashboard",
    "collect",
    "redaction",
    "jobs",
    "findings",
    "support-package",
    "date-ranges",
    "settings",
    "users-and-roles",
}
TOP_LEVEL_SLUGS = {"installation", "configuration", "architecture"}


def test_all_pages_covers_the_expected_set() -> None:
    slugs = {page.slug for page in all_pages()}
    assert slugs == USER_GUIDE_SLUGS | TOP_LEVEL_SLUGS


def test_user_guide_pages_are_grouped_and_top_level_pages_are_not() -> None:
    for page in all_pages():
        if page.slug in USER_GUIDE_SLUGS:
            assert page.group == "User guide"
        else:
            assert page.group is None


def test_readme_and_function_index_are_not_pages() -> None:
    # README.md and docs/functions.md are GitHub-only: a contributor
    # reference and the project's public face, not part of the user manual.
    assert get_page("readme") is None
    assert get_page("functions") is None


def test_roadmap_is_not_a_page() -> None:
    # roadmap.md documents the roadmap process itself, not how to run or
    # configure the app — deliberately excluded, see the module docstring.
    assert get_page("roadmap") is None


def test_unknown_slug_returns_none() -> None:
    assert get_page("nonexistent") is None


def test_readme_nav_line_is_stripped_from_mirrored_docs_pages() -> None:
    installation = get_page("installation")
    assert "back to the README" not in installation.html


def test_user_guide_pages_have_no_readme_nav_line_to_strip() -> None:
    # None of them ever had one — there is no GitHub page to return to — so
    # stripping is a no-op here rather than a special case.
    first_login = get_page("first-login")
    assert "back to the README" not in first_login.html
    assert first_login.html.startswith("<h1")


def test_gfm_callouts_become_styled_divs() -> None:
    # docs/installation.md has at least one [!WARNING] callout (the `down -v`
    # data-loss warning).
    installation = get_page("installation")
    assert 'class="callout callout-warning"' in installation.html
    # The raw GFM marker line must not leak through as literal text.
    assert "[!WARNING]" not in installation.html


def test_link_between_two_user_guide_pages_becomes_a_help_route() -> None:
    # first-login.md links to dashboard.md and settings.md.
    first_login = get_page("first-login")
    assert 'href="/help/dashboard"' in first_login.html
    assert 'href="/help/settings"' in first_login.html


def test_link_between_a_user_guide_page_and_a_top_level_page_would_use_slug() -> None:
    # No current page links from the user guide to Installation/Configuration/
    # Architecture, so exercise the mechanism directly rather than relying on
    # one appearing by coincidence in prose that could change.
    html = '<a href="installation.md#requirements">Installation</a>'
    rewritten = _rewrite_links(html, Path(__file__).parent.parent / "docs")
    assert 'href="/help/installation#requirements"' in rewritten


def test_link_with_anchor_to_an_unrendered_page_becomes_an_absolute_github_link() -> None:
    # configuration.md links to roadmap.md#which-xen-orchestra-account-to-use.
    # roadmap.md is not rendered, so this must resolve to an absolute GitHub
    # URL — including the docs/ prefix, since both files live there.
    configuration = get_page("configuration")
    assert (
        f'href="{GITHUB_BLOB_ROOT}docs/roadmap.md#which-xen-orchestra-account-to-use"'
        in configuration.html
    )


def test_link_to_functions_becomes_an_absolute_github_link() -> None:
    # architecture.md links to functions.md, which is GitHub-only now.
    architecture = get_page("architecture")
    assert f'href="{GITHUB_BLOB_ROOT}docs/functions.md"' in architecture.html
    # It leaves the app, so it opens in a new tab rather than replacing the
    # manual page the reader was on.
    assert f'<a href="{GITHUB_BLOB_ROOT}docs/functions.md" target="_blank"' in architecture.html


def test_search_finds_a_term_and_returns_a_snippet() -> None:
    results = search("redaction")
    assert results, "expected at least one page to mention redaction"
    slugs = {page.slug for page, _snippet in results}
    assert "redaction" in slugs
    for _page, snippet in results:
        assert snippet  # never an empty string


def test_search_is_case_insensitive() -> None:
    assert search("REDACTION") == search("redaction")


def test_search_blank_query_returns_nothing() -> None:
    assert search("") == []
    assert search("   ") == []


def test_search_no_match_returns_empty_list() -> None:
    assert search("xyzzy-not-a-real-word-in-these-docs") == []
