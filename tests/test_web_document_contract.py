"""Document decoding and navigation use the fetched document's semantics."""
import io
from email.message import Message
from types import SimpleNamespace

import pytest

from app.backend.tooling import web_page


def read(monkeypatch, markup, *, header='text/html', final='https://docs.example/redirected/index.html'):
    payload = markup if isinstance(markup, bytes) else markup.encode()
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = header
        def geturl(self):
            return final
    monkeypatch.setattr(web_page, 'checked_public_url', lambda value: value)
    monkeypatch.setattr(web_page, 'public_opener', lambda: SimpleNamespace(
        open=lambda *args, **kwargs: Response(payload)))
    return web_page.read_public_page('https://docs.example/start')


PROSE = '<p>The reference describes the public instrument calibration and measurement procedure. Results include the measured wavelength and its uncertainty.</p>'


def test_first_base_resolves_links_and_explicit_forms_after_redirect(monkeypatch):
    page = read(monkeypatch, '<base href="../manual/"><base href="https://wrong.example/">'
        + PROSE + '<a href="calibration.html">Calibration procedure</a>'
        '<form action="search" method="get"><input type="search" name="q"></form>')
    assert page['url'] == 'https://docs.example/redirected/index.html'
    assert page['links'][0]['url'] == 'https://docs.example/manual/calibration.html'
    assert page['search_forms'][0]['action'] == 'https://docs.example/manual/search'
    assert page['document_base_url'] == 'https://docs.example/manual/'


def test_template_base_and_bad_link_do_not_poison_readable_source(monkeypatch):
    page = read(monkeypatch, '<template><base href="https://wrong.example/"></template>'
        + PROSE + '<a href="http://[invalid">Broken</a>'
        '<a href="calibration.html">Calibration procedure</a>')
    assert len(page['links']) == 1
    assert page['links'][0]['url'] == 'https://docs.example/redirected/calibration.html'


def test_credentialed_links_are_not_exposed_as_discovery_targets(monkeypatch):
    page = read(monkeypatch, PROSE + '<a href="https://user:secret@docs.example/report">Report</a>')
    assert page['links'] == []


@pytest.mark.parametrize('declaration', [
    '<meta charset="windows-1252">',
    '<meta content="text/html; charset=windows-1252" http-equiv="Content-Type">',
])
def test_html_meta_charset_preserves_measured_units(monkeypatch, declaration):
    page = read(monkeypatch, (declaration + PROSE + '<p>Measurement: 25 °C; precision ±2 µm; price €30.</p>').encode('cp1252'))
    assert '25 °C; precision ±2 µm; price €30' in page['summary']
    assert page['decoding']['source'] == 'html_meta'
    assert page['decoding']['replacement_characters'] == 0


def test_bom_precedes_conflicting_transport_charset(monkeypatch):
    page = read(monkeypatch, ('\ufeff' + PROSE + '<p>বাংলা measurements: 25 °C.</p>').encode(),
        header='text/html; charset=windows-1252')
    assert 'বাংলা' in page['summary']
    assert page['decoding']['source'] == 'bom'


def test_unknown_charset_does_not_discard_valid_utf8_page(monkeypatch):
    page = read(monkeypatch, PROSE + '<p>Temperature: 25 °C.</p>',
        header='text/html; charset=not-a-real-charset')
    assert '25 °C' in page['summary']
    assert page['decoding']['source'] == 'utf8_default'


def test_missing_form_action_stays_on_document_not_base(monkeypatch):
    page = read(monkeypatch, '<base href="/manual/">' + PROSE
        + '<form method="get"><input name="keyword"><input type="submit" value="Search"></form>')
    assert page['search_forms'][0]['action'] == ''


def test_http_charset_precedes_meta_and_iso_label_uses_web_mapping(monkeypatch):
    page = read(monkeypatch, ('<meta charset="utf-8">' + PROSE
        + '<p>Calibration charge: €30.</p>').encode('cp1252'),
        header='text/html; charset=iso-8859-1')
    assert '€30' in page['summary']
    assert page['decoding']['source'] == 'http_header'


def test_meta_text_in_script_is_not_an_encoding_declaration(monkeypatch):
    page = read(monkeypatch, '<script>const example = \'<meta charset="cp1252">\';</script>'
        + '<meta charset="utf-8">' + PROSE + '<p>বাংলা calibration report.</p>')
    assert 'বাংলা' in page['summary']
    assert page['decoding']['encoding'] == 'utf-8'


@pytest.mark.parametrize('label', ['base64_codec', 'utf-7', 'rot_13', 'unicode_escape', 'raw_unicode_escape', 'punycode'])
def test_non_web_codecs_fall_back_without_executing_transforms(monkeypatch, label):
    page = read(monkeypatch, PROSE, header='text/html; charset=' + label)
    assert page['decoding']['encoding'] == 'utf-8'
    assert page['decoding']['ignored_labels'] == [label]


def test_redirect_base_discovery_and_decoding_reach_claim_provenance(monkeypatch):
    from app.backend.research.loop import ResearchLoop
    from app.backend.research.ledger import Budget
    root = 'https://docs.example/start'
    leaf = 'https://docs.example/manual/calibration.html'
    visited = []
    def fetch(url):
        visited.append(url)
        if url == root:
            return read(monkeypatch, '<base href="/manual/">' + PROSE
                + '<a href="calibration.html">Calibration measurement temperature</a>')
        assert url == leaf
        return read(monkeypatch, ('<meta charset="cp1252">' + PROSE
            + '<p>The calibration measurement temperature is 25 °C and uncertainty is ±2 µm.</p>').encode('cp1252'),
            final=leaf)
    report = ResearchLoop('Find calibration measurement temperature and uncertainty',
        search=lambda _: [{'url': root}], read=fetch,
        budget=Budget(max_queries=1, max_sources=2, coverage_target=99)).run()
    assert visited == [root, leaf]
    assert any('25 °C' in claim['text'] and '±2 µm' in claim['text'] for claim in report['claims'])
    source = next(s for s in report['sources'] if s['url'] == leaf)
    assert source['retrieval_details']['decoding']['source'] == 'html_meta'
    assert source['retrieval_details']['discovered_from'] == 'https://docs.example/redirected/index.html'
