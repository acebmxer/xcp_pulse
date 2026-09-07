"""The redaction preview page.

The engine's own behaviour is covered in test_redact.py. These check the page
around it: that it renders, that a paste comes back masked, and — the property
the page exists for — that the pasted text is not stored anywhere.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.redact import RULES


def test_page_renders_with_a_sample_ready(logged_in: TestClient) -> None:
    response = logged_in.get("/redaction")
    assert response.status_code == 200
    body = response.text
    assert "Redaction" in body
    # The sample is pre-filled so the page explains itself on first load.
    assert "trackid=" in body
    assert "IPv4 addresses" in body


def test_pasted_text_comes_back_masked(logged_in: TestClient) -> None:
    response = logged_in.post(
        "/redaction",
        data={"text": "host 10.20.30.41 trackid=abc123 password=hunter2"},
    )
    assert response.status_code == 200
    body = response.text
    assert "[IPv4]" in body
    assert "[TOKEN]" in body
    assert "[SECRET]" in body
    # The values themselves must not appear in the result block. They are still
    # in the textarea, which is the input the operator pasted.
    assert "hunter2" not in body.split("Result")[1]


def test_hit_counts_are_shown(logged_in: TestClient) -> None:
    response = logged_in.post("/redaction", data={"text": "10.0.0.1 and 10.0.0.2"})
    assert response.status_code == 200
    assert "2 values masked" in response.text


def test_text_that_matches_nothing_says_so(logged_in: TestClient) -> None:
    response = logged_in.post("/redaction", data={"text": "starting up, all well"})
    assert response.status_code == 200
    assert "Nothing in that text matched a rule." in response.text


def test_empty_paste_is_not_an_error(logged_in: TestClient) -> None:
    response = logged_in.post("/redaction", data={"text": ""})
    assert response.status_code == 200


def test_an_oversized_paste_is_truncated_and_said_so(logged_in: TestClient) -> None:
    from app.routes.redaction import MAX_PREVIEW_CHARS

    response = logged_in.post(
        "/redaction",
        data={"text": "10.0.0.1 " * (MAX_PREVIEW_CHARS // 4)},
    )
    assert response.status_code == 200
    assert "were previewed" in response.text


def test_nothing_pasted_is_written_to_the_database(logged_in: TestClient, settings) -> None:
    """The page is for text people would rather not leave behind."""
    secret = "trackid=deadbeefcafe1234"
    logged_in.post("/redaction", data={"text": secret})

    db_bytes = (settings.data_dir / "xcp-pulse.db").read_bytes()
    assert b"deadbeefcafe1234" not in db_bytes
    for path in settings.data_dir.rglob("*"):
        if path.is_file():
            assert b"deadbeefcafe1234" not in path.read_bytes(), f"leaked into {path}"


def test_the_preview_requires_a_login(client: TestClient) -> None:
    assert client.get("/redaction").status_code == 303
    assert client.post("/redaction", data={"text": "10.0.0.1"}).status_code == 303


def test_the_stylesheet_url_carries_a_cache_busting_token(logged_in: TestClient) -> None:
    """A CSS change must actually reach the browser.

    StaticFiles sends an ETag and Last-Modified but no Cache-Control, so a
    browser may reuse a cached stylesheet without revalidating — a fix then
    reaches the container and never the page, which is invisible server-side.
    """
    body = logged_in.get("/redaction").text
    assert "/static/style.css?v=" in body


# ---- switching rules on and off ----


def _ticked(body: str) -> set[str]:
    """The rule names whose checkbox is ticked, read out of the rendered form.

    Read by pattern rather than by counting the word "checked", which also
    appears in prose on the page.
    """
    return set(re.findall(r'value="([a-z0-9]+)"\s+checked', body))


def _result_block(body: str) -> str:
    """Only the redacted output, not the rule descriptions.

    Every placeholder appears in the rules list as documentation, so asserting
    against the whole page would pass whatever redaction did.
    """
    return body.split('<pre class="mono redaction-result">')[1].split("</pre>")[0]


def test_every_rule_starts_switched_on(logged_in: TestClient) -> None:
    """The safe default is everything masked."""
    body = logged_in.get("/redaction").text
    assert _ticked(body) == {rule.name for rule in RULES}
    assert "rules switched off" not in body


def test_switching_a_rule_off_stops_it_masking(logged_in: TestClient) -> None:
    logged_in.post(
        "/redaction/rules",
        data={"rule": ["secret", "trackid", "email", "mac", "ipv6", "uuid", "hostname"]},
    )
    result = _result_block(logged_in.post("/redaction", data={"text": "host 10.20.30.41"}).text)
    assert "[IPv4]" not in result
    assert "10.20.30.41" in result


def test_a_rule_switched_off_survives_a_reload(logged_in: TestClient) -> None:
    """The point of storing it: a choice made once stays made."""
    logged_in.post("/redaction/rules", data={"rule": ["ipv4"]})
    body = logged_in.get("/redaction").text
    assert _ticked(body) == {"ipv4"}
    assert f"{len(RULES) - 1} rules switched off" in body


def test_switching_a_rule_back_on_masks_again(logged_in: TestClient) -> None:
    logged_in.post("/redaction/rules", data={"rule": ["secret"]})
    assert "[IPv4]" not in _result_block(
        logged_in.post("/redaction", data={"text": "10.0.0.1"}).text
    )

    logged_in.post("/redaction/rules", data={"rule": [rule.name for rule in RULES]})
    assert "[IPv4]" in _result_block(logged_in.post("/redaction", data={"text": "10.0.0.1"}).text)


def test_saving_with_nothing_ticked_switches_everything_off(logged_in: TestClient) -> None:
    """An unticked box posts nothing, so an empty form means all off."""
    assert logged_in.post("/redaction/rules", data={}).status_code == 303

    body = logged_in.get("/redaction").text
    assert _ticked(body) == set()
    assert f"{len(RULES)} rules switched off" in body

    result = _result_block(
        logged_in.post("/redaction", data={"text": "10.0.0.1 password=hunter2"}).text
    )
    assert "10.0.0.1" in result
    assert "hunter2" in result


def test_an_unknown_rule_name_is_ignored(logged_in: TestClient) -> None:
    """A stale or hand-edited form must not create a rule that does not exist."""
    assert logged_in.post("/redaction/rules", data={"rule": ["ipv4", "nope"]}).status_code == 303
    assert _ticked(logged_in.get("/redaction").text) == {"ipv4"}


def test_saving_the_rules_requires_a_login(client: TestClient) -> None:
    response = client.post("/redaction/rules", data={"rule": ["ipv4"]})
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
