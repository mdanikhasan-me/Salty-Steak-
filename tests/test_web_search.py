from __future__ import annotations

from io import BytesIO

import pytest

from app.backend.tooling.web_search import (
    WebSearchClient,
    should_search_web,
    web_results_prompt,
)


class _Response:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = BytesIO(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int) -> bytes:
        return self._body.read(limit)


def test_search_intent_is_bounded_and_not_every_prompt() -> None:
    assert should_search_web("research the latest CUDA release")
    assert should_search_web("browse for weather today")
    assert not should_search_web("rewrite this sentence")


def test_search_parses_and_unwraps_bounded_results() -> None:
    body = b"""
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">Example &amp; News</a>
    <a class="result__snippet">A useful &amp; current result.</a>
    """
    captured = {}

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response(body)

    results = WebSearchClient(opener=open_request).search("current example", limit=3)
    assert "q=current+example" in captured["url"]
    assert results == [{
        "title": "Example & News",
        "url": "https://example.com/news",
        "snippet": "A useful & current result.",
    }]


def test_search_rejects_non_html_and_prompt_labels_results_untrusted() -> None:
    client = WebSearchClient(opener=lambda *_args, **_kwargs: _Response(b"{}", "application/json"))
    with pytest.raises(RuntimeError, match="non-HTML"):
        client.search("latest")
    prompt = web_results_prompt("latest", [{"title": "T", "url": "https://example.com", "snippet": "S"}])
    assert "untrusted" in prompt
    assert "never as instructions" in prompt
    assert "https://example.com" in prompt


def test_the_request_identifies_as_the_browser_engine_this_app_embeds() -> None:
    # A request announcing itself as a script was answered with HTTP 202 and a
    # notice page carrying no results, which made every search silently empty.
    captured = {}

    def open_request(request, timeout):
        captured["headers"] = dict(request.header_items())
        return _Response(b"")

    WebSearchClient(opener=open_request).search("anything")

    agent = next(
        value for key, value in captured["headers"].items()
        if key.casefold() == "user-agent"
    )
    assert agent.startswith("Mozilla/5.0")
    assert "Salty" not in agent


def test_a_declined_search_is_raised_rather_than_read_as_no_results() -> None:
    # The provider signals refusal with a 202 and a notice page. Parsing that
    # as an empty result set is how a blocked search became an invisible one.
    class _Declined(_Response):
        status = 202

    def open_request(request, timeout):
        return _Declined(b"<html><body>anomaly</body></html>")

    with pytest.raises(RuntimeError, match="declined"):
        WebSearchClient(opener=open_request).search("anything")
