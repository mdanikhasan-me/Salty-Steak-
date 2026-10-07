"""Static-source visibility must not admit hidden facts or discovery targets."""
import pytest
from app.backend.tooling.web_page import PageText


@pytest.mark.parametrize('attributes', [
    'hidden', 'hidden="false"', 'aria-hidden="true"',
    'style="display: none"', 'style="DISPLAY : none !important; color: red"',
    'style="visibility: hidden;"',
])
def test_hidden_subtrees_do_not_become_research_evidence(attributes):
    parser = PageText()
    parser.feed(f'''<title>Actual report</title><h1>Current figures</h1>
        <div {attributes}><p>Outdated total: 999</p>
        <a href="/obsolete">Obsolete report</a>
        <form action="/fake-search"><input name="q"></form>
        <table><tr><th>Year</th><th>Total</th></tr><tr><td>2026</td><td>999</td></tr></table>
        </div><p>Current total: 42</p><a href="/current">Current report</a>''')
    text = ''.join(parser.parts)
    assert '999' not in text and '42' in text
    assert parser.links == [{'url':'/current','title':'Current report'}]
    assert parser.forms == [] and parser.table_rows == []
    assert ''.join(parser.title) == 'Actual report'


def test_hidden_void_nodes_and_nested_spans_do_not_hide_following_content():
    parser = PageText()
    parser.feed('''<p>Read <span hidden><b>not this</b></span>this.</p>
        <input hidden><br hidden><p aria-hidden="false">Keep this.</p>
        <a href="/actual">Actual<span hidden>old label</span> link</a>''')
    assert 'not this' not in ''.join(parser.parts)
    assert 'Keep this.' in ''.join(parser.parts)
    assert parser.links == [{'url':'/actual','title':'Actual link'}]


def test_hidden_template_content_cannot_change_visible_table_headers():
    parser = PageText()
    parser.feed('''<h2>Visible table</h2><table><tr><th>Name</th><th>Count</th></tr>
        <template><table><tr><th>Wrong</th></tr><tr><td>999</td></tr></table></template>
        <tr><td>Orchid</td><td>42</td></tr></table>''')
    assert parser.table_rows == ['Visible table — Name: Orchid | Count: 42']


def test_inline_visibility_respects_later_values_and_important():
    parser = PageText()
    parser.feed('''<p style="display:none;display:block">Shown</p>
        <p style="display:none!important;display:block">Not shown</p>
        <p style="display:none!important;display:block!important">Visible</p>''')
    text = ''.join(parser.parts)
    assert 'Shown' in text and 'Visible' in text and 'Not shown' not in text


def test_hidden_conflicting_fact_does_not_reach_parsed_claims(monkeypatch):
    from email.message import Message
    from io import BytesIO
    from types import SimpleNamespace
    from app.backend.tooling import web_page
    from app.backend.research.loop import statements_from_page
    document = '''<title>Inventory report</title><h2>Inventory</h2>
      <div hidden><p>The current inventory contains 999 orchid samples.</p>
      <a href="/old-report">Old inventory report</a></div>
      <p>The current inventory contains 42 orchid samples. Every sample was counted
      individually during the published annual survey. The report distinguishes
      living specimens from archived observations and uses consistent counting units.</p>'''
    class Response(BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'
        def geturl(self): return 'https://reports.example/inventory'
    monkeypatch.setattr(web_page, 'checked_public_url', lambda value:value)
    monkeypatch.setattr(web_page, 'public_opener', lambda:SimpleNamespace(
        open=lambda *args,**kwargs:Response(document.encode())))
    page = web_page.read_public_page('https://reports.example/inventory', question='How many orchid samples?')
    claims = statements_from_page(page, question='How many orchid samples?')
    assert any('42 orchid samples' in claim for claim in claims)
    assert all('999' not in claim for claim in claims)
    assert page['links'] == []
    assert page['visibility'] == {'method':'explicit_html_and_inline_styles',
                                  'hidden_elements':1, 'computed_styles':False}
    from app.backend.research.loop import ResearchLoop
    from app.backend.research.ledger import Budget, ResearchLedger
    loop = ResearchLoop('How many orchid samples?',
        search=lambda query:[{'url':page['url'],'title':page['title']}],
        read=lambda url:page, budget=Budget(max_queries=1,max_sources=1,validation_rounds=0))
    report = loop.run()
    assert report['sources'][0]['retrieval_details']['visibility'] == page['visibility']
    restored = ResearchLedger.from_checkpoint(loop.ledger.checkpoint())
    assert restored.sources['src-1'].retrieval_details['visibility'] == page['visibility']


def test_reader_and_summary_hashes_keep_separate_scopes_through_ledger():
    import hashlib
    from app.backend.research.loop import ResearchLoop, validate_page
    from app.backend.research.ledger import Budget
    text = 'The measured inventory contains exactly 42 orchid samples in this report.'
    raw_hash = hashlib.sha256(('<p>'+text+'</p>').encode()).hexdigest()
    page = {'url':'https://reports.example/inventory','title':'Inventory', 'summary':text,
            'content_sha256':raw_hash,'hash_scope':'response_body_bytes'}
    validated = validate_page(page,page['url'])
    assert validated['content_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert validated['hash_scope'] == 'validated_summary_utf8'
    assert validated['reader_content_sha256'] == raw_hash
    assert validate_page(validated,page['url'])['reader_content_sha256'] == raw_hash
    report = ResearchLoop('How many orchid samples?',search=lambda q:[{'url':page['url']}],
        read=lambda url:page,budget=Budget(max_sources=1,max_queries=1,validation_rounds=0)).run()
    details = report['sources'][0]['retrieval_details']
    assert details['reader_content_sha256'] == raw_hash
    assert details['reader_hash_scope'] == 'response_body_bytes'
    assert details['hash_scope'] == 'validated_summary_utf8'
