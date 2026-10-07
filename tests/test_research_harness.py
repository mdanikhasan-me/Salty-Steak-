"""Regressions at the reader -> evidence boundary, without network dependencies."""
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

from app.backend.chat.service import ChatService
from app.backend.research.browser_reader import read_browser_source
from app.backend.research.loop import ResearchLoop, statements_from_page
from app.backend.research.ledger import Budget
from app.backend.tooling import web_page


def test_long_static_table_row_keeps_subject_and_all_year_bindings(monkeypatch):
    years = range(2000, 2026)
    html = ('<h2>Annual visits</h2><table><tr><th>Park</th>'
            + ''.join(f'<th>{year}</th>' for year in years) + '</tr><tr><td>Example Park</td>'
            + ''.join(f'<td>{year + 1000:,}</td>' for year in years) + '</tr></table>')

    class Response(BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'

        def geturl(self):
            return 'https://stats.example/report'

    monkeypatch.setattr(web_page, 'checked_public_url', lambda url: url)
    monkeypatch.setattr(web_page, 'public_opener', lambda: SimpleNamespace(
        open=lambda *a, **kw: Response(html.encode())))
    page = web_page.read_public_page('https://stats.example/report', question='2024 visits Example Park')
    claims = statements_from_page(page, question='2024 visits Example Park')
    assert page['table_rows_read'] == 1
    assert len(page['table_rows'][0]) > 300
    assert any('Park: Example Park' in claim and '2024: 3,024' in claim and '2025: 3,025' in claim
               for claim in claims)


def test_browser_selects_requested_fact_past_output_limit():
    fact = 'The Zephyr instrument correction is exactly 37 units according to the calibration record.'
    text = 'Unrelated navigation and site history.\n' * 1100 + fact + '\n'
    calls = []

    def call(command, **args):
        if command == 'open_url':
            return {'status': 'succeeded', 'tab': 'tab-1'}
        assert args['tab'] == 'tab-1'
        start = args['text_offset']
        end = min(len(text), start + args['text_limit'])
        calls.append(start)
        return {'status': 'succeeded', 'url': 'https://archive.example/record',
                'summary': text[start:end], 'text_length': len(text),
                'next_text_offset': end if end < len(text) else None, 'next_offset': None}

    page = read_browser_source(call, 'https://archive.example/record', question='Zephyr correction')
    assert fact in page['summary']
    assert max(calls) > 24000
    assert len(page['summary']) <= 24000 and page['truncated']
    assert page['scanned_text_characters'] == len(text)
    assert page['page_text_characters'] == len(text)
    assert all(span['end'] <= page['source_text_characters'] for span in page['selected_ranges'])
    assert any(fact in claim for claim in statements_from_page(page, question='Zephyr correction'))


def test_browser_chunk_boundary_does_not_split_fact_or_numeric_value():
    prefix = 'Navigation.\n' * 665 + 'x' * 7 + '\n'
    fact = 'The station measured 12345 units in the original calibration record.'
    text = prefix + fact

    def call(command, **args):
        if command == 'open_url':
            return {'status': 'succeeded'}
        start = args['text_offset']
        end = min(start + args['text_limit'], len(text))
        return {'status': 'succeeded', 'url': 'https://archive.example/record',
                'summary': text[start:end], 'text_length': len(text),
                'next_text_offset': end if end < len(text) else None}

    page = read_browser_source(call, 'https://archive.example/record')
    assert page['summary'] == text
    assert fact in statements_from_page(page, question='station calibration units')


@pytest.mark.parametrize('failed_command', ['open_url', 'query'])
def test_browser_search_failure_is_not_reported_as_zero_results(failed_command):
    def invoke(request):
        if request['arguments']['command'] == failed_command:
            return {'status': 'failed', 'error': 'Provider navigation failed'}
        return {'status': 'succeeded', 'tab': 'search-tab'}

    service = SimpleNamespace(web_search=SimpleNamespace(search=lambda *a, **kw: []),
        automation=SimpleNamespace(invoke=invoke),
        granted_automation_capabilities=lambda: ['browser.control'])
    with pytest.raises(RuntimeError, match='Provider navigation failed'):
        ChatService._search_web(service, 'Zephyr correction')


def test_browser_search_reads_the_tab_it_opened():
    def invoke(request):
        args = request['arguments']
        if args['command'] == 'open_url':
            return {'status': 'succeeded', 'tab': 'search-tab'}
        assert args['tab'] == 'search-tab'
        return {'status': 'succeeded', 'matches': [
            {'href': 'https://archive.example/record', 'name': 'Zephyr record'}]}

    service = SimpleNamespace(web_search=SimpleNamespace(search=lambda *a, **kw: []),
        automation=SimpleNamespace(invoke=invoke),
        granted_automation_capabilities=lambda: ['browser.control'])
    assert ChatService._search_web(service, 'Zephyr correction')[0]['url'] == 'https://archive.example/record'


def test_research_report_keeps_reader_limits_and_fallback_provenance():
    page = {'url': 'https://archive.example/record',
            'summary': 'Zephyr instrument correction is precisely 37 units.',
            'retrieval': 'owned_browser_paginated', 'fallback_reason': 'HTTP Error 503',
            'source_text_characters': 44000, 'scanned_text_characters': 32000,
            'table_rows_read': 2, 'truncated': True, 'read_limit': {'max_pages': 4}}
    result = ResearchLoop('Zephyr correction',
        search=lambda _: [{'url': page['url']}], read=lambda _: page,
        budget=Budget(max_queries=1)).run()
    metadata = result['sources'][0]['retrieval_details']
    assert metadata['fallback_reason'] == 'HTTP Error 503'
    assert metadata['scanned_text_characters'] == 32000
    assert metadata['truncated'] and metadata['table_rows_read'] == 2


def test_error_status_documentation_is_not_an_error_response():
    from app.backend.research.loop import validate_page
    page = {'url': 'https://docs.example/status/404', 'title': '404 Not Found - HTTP reference',
            'summary': 'HTTP reference\n404 Not Found\nThe HTTP 404 Not Found status indicates '
                       'that the server cannot find the requested resource.'}
    assert validate_page(page, page['url'])['validation'] == 'validated'


@pytest.mark.parametrize('banner', ['404 Not Found', '502 Bad Gateway', '504 Gateway Time-out',
                                  'Just a moment...', 'Invite Invalid'])
def test_actual_error_banners_are_still_rejected(banner):
    from app.backend.research.loop import validate_page
    with pytest.raises(ValueError, match='invalid page'):
        validate_page({'title': banner, 'summary': banner + '. Please try your request again later.'},
                      'https://unavailable.example')


def test_inline_svg_titles_do_not_pollute_document_title():
    parser = web_page.PageText()
    parser.feed('<title>Original document</title><svg><title>Icon description</title></svg>'
                '<p>Original article text.</p>')
    assert ''.join(parser.title) == 'Original document'
    assert 'Icon description' not in ''.join(parser.parts)


@pytest.mark.parametrize('status', [404, 410])
def test_missing_resource_preserves_http_error_without_rendering_browser_error(monkeypatch, status):
    from urllib.error import HTTPError

    def missing(url):
        raise HTTPError(url, status, 'Missing resource', {}, None)

    def no_browser(_):
        raise AssertionError('A missing resource must not render a browser error page')

    monkeypatch.setattr(web_page, 'read_public_page', missing)
    service = SimpleNamespace(automation=SimpleNamespace(invoke=no_browser),
        granted_automation_capabilities=lambda: ['browser.control'])
    with pytest.raises(RuntimeError, match=str(status)):
        ChatService._read_source(service, 'https://archive.example/missing')


def test_range_exhaustion_falls_back_to_one_complete_bounded_pdf(monkeypatch):
    from app.backend.tooling import pdf_text
    from tests.test_pdf_retrieval import pdf_bytes
    payload = pdf_bytes('The instrument calibration requires a correction of precisely 37 units. ' * 3)
    payload += b'\n' * 1_000_000
    calls = []

    class Response(BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/pdf'
        headers['Content-Length'] = str(len(payload))
        headers['Accept-Ranges'] = 'bytes'
        headers['ETag'] = 'stable'

        def geturl(self):
            return 'https://archive.example/report.pdf'

    def open_response(*args, **kwargs):
        calls.append('full')
        return Response(payload)

    def no_ranges(*args):
        calls.append('ranges')
        raise ValueError('PDF selective-reading budget exhausted')

    monkeypatch.setattr(web_page, 'checked_public_url', lambda url: url)
    monkeypatch.setattr(web_page, 'public_opener', lambda: SimpleNamespace(open=open_response))
    monkeypatch.setattr(pdf_text, 'read_remote_pdf', no_ranges)
    page = web_page.read_public_page('https://archive.example/report.pdf')
    assert calls == ['full', 'ranges', 'full']
    assert page['retrieval'] == 'public_pdf'
    assert 'precisely 37 units' in page['summary'] and page['page_count'] == 1
    assert 'selective-reading budget exhausted' in page['fallback_reason']


def test_pdf_extraction_failure_does_not_fall_back_to_empty_browser_plugin(monkeypatch):
    def no_text(url):
        raise web_page.PDFReadError('No text layer; OCR is required')

    def no_browser(_):
        raise AssertionError('Browser PDF plugin cannot substitute for text extraction')

    monkeypatch.setattr(web_page, 'read_public_page', no_text)
    service = SimpleNamespace(automation=SimpleNamespace(invoke=no_browser),
        granted_automation_capabilities=lambda: ['browser.control'])
    with pytest.raises(RuntimeError, match='OCR is required'):
        ChatService._read_source(service, 'https://archive.example/scan.pdf')


@pytest.mark.parametrize(('text', 'expected'), [
    ('1 Introduction\nThe article describes its method.\n2', '2'),
    ('1 Introduction\nThe article describes its method.', None),
    ('IV RESULTS\nMeasurements and conclusions.', None),
    ('x\nCorrections to the observatory readings.', 'x'),
    ('Methods\nMeasurements and conclusions.\niv', 'iv'),
])
def test_pdf_printed_label_never_uses_section_number(text, expected):
    from app.backend.tooling.pdf_text import printed_page_label
    assert printed_page_label(text) == expected


def test_http_200_search_challenge_is_not_a_zero_result_search():
    from tests.test_web_search import _Response
    from app.backend.tooling.web_search import WebSearchClient
    client = WebSearchClient(opener=lambda *a, **kw: _Response(
        b'<html><div class="anomaly-modal">Unfortunately, bots use DuckDuckGo too.</div></html>'))
    with pytest.raises(RuntimeError, match='verification challenge'):
        client.search('original paper')


def test_browser_search_challenge_is_explicit_failure():
    def invoke(request):
        command = request['arguments']['command']
        return {'status': 'succeeded', 'tab': 'search-tab', 'matches': [],
                'summary': 'Unfortunately, bots use DuckDuckGo too.' if command == 'read_page' else ''}

    service = SimpleNamespace(web_search=SimpleNamespace(search=lambda *a, **kw: []),
        automation=SimpleNamespace(invoke=invoke),
        granted_automation_capabilities=lambda: ['browser.control'])
    with pytest.raises(RuntimeError, match='human verification'):
        ChatService._search_web(service, 'original paper')


def test_publisher_branded_verification_page_is_not_evidence():
    from app.backend.research.loop import validate_page
    with pytest.raises(ValueError, match='verification challenge'):
        validate_page({'title': 'Verifying your browser | Publisher',
            'summary': 'Publisher\nVerifying your browser\nComplete the check below to continue.\n'
                       'Please complete the verification above. Sign in to skip this check.'},
            'https://publisher.example/challenge')


def test_malformed_coverage_gets_one_checked_format_repair():
    import json
    from app.backend.research.coverage import assess_coverage
    calls = []

    def generate(messages):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({'requirements': ['architecture'], 'missing': [], 'query': None})
        assert 'failed format validation' in messages[-1]['content']
        return json.dumps({'requirements': [['architecture', ['clm-1']]], 'missing': [], 'query': None})

    result = assess_coverage('Which architecture?', [{'claim': 'clm-1', 'text': 'The Transformer architecture.'}], generate)
    assert result['sufficient'] and result['attempts'] == 2 and len(calls) == 2
    assert result['format_errors']


def test_repaired_coverage_cannot_invent_claim_ids_or_loop():
    from app.backend.research.coverage import assess_coverage
    calls = []

    def generate(messages):
        calls.append(messages)
        return '{"requirements":[["architecture",["invented"]]],"missing":[],"query":null}'

    result = assess_coverage('Which architecture?', [{'claim': 'clm-1', 'text': 'A related fact.'}], generate)
    assert not result['sufficient'] and result['status'] == 'unavailable'
    assert result['error'] == 'Unknown claim citation' and len(calls) == 2


def test_coverage_repair_shares_original_deadline(monkeypatch):
    from app.backend.chat import runners
    from app.backend.research.ledger import ResearchLedger
    ledger = ResearchLedger('Which architecture?')
    source = ledger.add_source('https://paper.example/original')
    ledger.add_claim('The proposed architecture is the Transformer.', source.source_id)
    clock = [1000.0]
    monkeypatch.setattr(runners.time, 'monotonic', lambda: clock[0])
    budgets = []

    def generate(messages, *, time_budget_seconds):
        budgets.append(time_budget_seconds)
        clock[0] += 25
        return '{}'

    runner = runners.LiveRunners(generate_research_review=generate)
    result = runner._assess_coverage(ledger)
    assert len(budgets) == 2 and budgets[1] == budgets[0] - 25
    assert not result['sufficient']


def test_pdf_wrapped_fact_keeps_subject_exception_and_page_together():
    fact = ('We propose a new simple network architecture, the Transformer,\n'
            'based solely on attention mechanisms, dispensing with recurrence and convolutions\n'
            'entirely.')
    page = {'pages': [{'page': 1, 'text': 'Abstract\n' + fact}]}
    claims = statements_from_page(page, question='proposed architecture mechanisms dispenses')
    assert any('Transformer' in text and 'recurrence and convolutions entirely.' in text
               and '[PDF page 1]' in text for text in claims)


def test_pdf_numeric_rows_are_not_joined_to_neighboring_claims():
    from app.backend.research.passages import pdf_blocks
    text = '1 Results\nStation A 2023 123\nStation B 2024 456\nThe correction was applied to\nall readings.'
    blocks = pdf_blocks(text)
    assert 'Station A 2023 123' in blocks and 'Station B 2024 456' in blocks
    assert 'The correction was applied to all readings.' in blocks


def test_empty_site_search_cannot_echo_question_into_evidence():
    from app.backend.research.loop import validate_page
    page = {'title': 'Search | Archive',
            'summary': 'Sorry, your query for all: proposed architecture and original PDF produced no results. '
                       'Try using fewer words or different search operators.'}
    with pytest.raises(ValueError, match='empty search page'):
        validate_page(page, 'https://archive.example/search?q=architecture')


def test_coverage_source_interning_is_lossless_and_reduces_repeated_metadata():
    import json
    from app.backend.research.coverage import coverage_packet
    url='https://reports.example/viewer?'+('report_parameter=long&'*20)
    findings=[{'claim':f'clm-{i}','text':f'Fact {i} retains its year and value.',
               'evidence':[{'url':url},{'url':'https://original.example/paper'}],
               'disputed':i==1} for i in range(20)]
    packet=coverage_packet('Compare the facts',findings)
    restored=[{**item,'sources':[packet['sources'][ref] for ref in item['sources']]}
              for item in packet['evidence']]
    expected=[{'id':c['claim'],'text':c['text'],'sources':[s['url'] for s in c['evidence']],
               'disputed':c['disputed']} for c in findings]
    assert restored==expected
    assert len(json.dumps(packet)) < len(json.dumps({'question':'Compare the facts','evidence':expected}))/2


def test_browser_waits_for_sparse_client_rendering_on_same_tab(monkeypatch):
    from app.backend.research import browser_reader
    monkeypatch.setattr(browser_reader.time,'sleep',lambda _:None)
    calls=[]
    content='The delayed page contains the original quote and its author. '*4
    def call(command,**args):
        if command=='open_url':return {'status':'succeeded','tab':'owned-7'}
        calls.append(args)
        assert args['tab']=='owned-7'
        text='Navigation only.' if len(calls)==1 else content
        return {'status':'succeeded','url':'https://dynamic.example/record','summary':text,'text_length':len(text)}
    result=read_browser_source(call,'https://dynamic.example/record')
    assert result['summary']==content and len(calls)==2


def test_sparse_render_wait_is_bounded_and_does_not_invent_text(monkeypatch):
    from app.backend.research import browser_reader
    monkeypatch.setattr(browser_reader.time,'sleep',lambda _:None)
    calls=[]
    def call(command,**args):
        if command=='open_url':return {'status':'succeeded'}
        calls.append(command)
        return {'status':'succeeded','url':'https://dynamic.example/record','summary':'Empty shell.','text_length':12}
    result=read_browser_source(call,'https://dynamic.example/record')
    assert result['summary']=='Empty shell.' and len(calls)==31 and result['render_incomplete']
    from app.backend.research.loop import validate_page
    with pytest.raises(ValueError,match='too sparse'):
        validate_page(result,'https://dynamic.example/record')
