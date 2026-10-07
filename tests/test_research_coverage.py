import json


def test_coverage_timeout_preserves_unknown_coverage_without_retrying_transport():
    from app.backend.research.coverage import assess_coverage
    calls=[]
    def timeout(messages):
        calls.append(messages)
        raise TimeoutError('review deadline exhausted')
    result=assess_coverage('question',[{'claim':'clm-1','text':'Observed fact'}],timeout)
    assert result['status']=='unavailable' and not result['sufficient']
    assert result['failure_kind']=='review_transport' and len(calls)==1


def test_coverage_does_not_swallow_user_cancellation():
    from app.backend.research.coverage import assess_coverage
    import pytest
    class Cancelled(RuntimeError):pass
    def cancel(messages):raise Cancelled('User stopped')
    with pytest.raises(Cancelled):assess_coverage('question',[],cancel)


def test_review_timeout_does_not_discard_readable_research_sources():
    from app.backend.chat.runners import LiveRunners
    def review(*args,**kwargs):raise TimeoutError('review deadline')
    def generate(messages):
        if messages[0]['content'].startswith('Reply with one JSON object'):
            return '{"query":null}'
        return 'The survey counted 42 orchid samples. [Report](https://reports.example/orchids)'
    runner=LiveRunners(generate=generate,generate_research_review=review,
        search=lambda query:[{'url':'https://reports.example/orchids'}],
        read=lambda url:{'url':url,'title':'Orchid report',
                         'summary':'The survey counted 42 orchid samples in the documented collection.'},
        research_profile='instant')
    result=runner.run_research(decision={},request='How many orchid samples were counted?')
    assert result['status']=='completed'
    assert '42' in result['answer']
    assert result['sources'][0]['url']=='https://reports.example/orchids'
    assert result['research']['coverage_assessment']['status']=='unavailable'
    assert not result['research']['evidence_sufficient']


def test_runner_returns_exact_evidence_used_for_answer_not_a_second_ranking(monkeypatch):
    from app.backend.chat import runners as module
    from app.backend.chat.runners import LiveRunners
    seen=[]
    original=module._select_finaliser_findings
    def select(question,findings,**kwargs):
        if kwargs.get('limit')==25:
            raise AssertionError('A second ranking can discard evidence used by the answer')
        return original(question,findings,**kwargs)
    monkeypatch.setattr(module,'_select_finaliser_findings',select)
    def generate(messages):
        if messages[0]['content'].startswith('Reply with one JSON object'):return '{"query":null}'
        seen.extend(json.loads(messages[-1]['content'])['findings'])
        return 'The survey counted 42 orchid samples. [Report](https://reports.example/orchids)'
    runner=LiveRunners(generate=generate,
        search=lambda query:[{'url':'https://reports.example/orchids'}],
        read=lambda url:{'url':url,'title':'Orchid report',
            'summary':'The survey counted 42 orchid samples in the documented collection.'})
    result=runner.run_research(decision={},request='How many orchid samples were counted?')
    assert seen
    assert [c['claim'] for c in result['claims']]==[c['claim'] for c in seen]
    assert all(c['evidence'] for c in result['claims'])
import pytest
from app.backend.research.coverage import assess_coverage
from app.backend.research.loop import ResearchLoop
from app.backend.research.ledger import Budget,ResearchLedger

@pytest.mark.parametrize('value',[
    {'requirements':[{'need':'requested date','claims':['invented']}],'missing':[],'query':None},
    {'requirements':[],'missing':[],'query':None},
    {'requirements':[{'need':'requested date','claims':[]}],'missing':[],'query':None},
])
def test_missing_or_invented_evidence_never_passes_coverage(value):
    result=assess_coverage('Find the date',[{'claim':'clm-1','text':'Related topic only.'}],lambda _:json.dumps(value))
    assert not result['sufficient']


def test_review_uses_requested_facets_and_only_known_claim_ids():
    result=assess_coverage('Find date and venue',[{'claim':'clm-1','text':'Date is June 1.'}],
        lambda _:json.dumps({'requirements':[{'need':'date','claims':['clm-1']},
            {'need':'venue','claims':[]}],'missing':['venue'],'query':'festival venue June 1'}))
    assert result['missing']==['venue'] and result['query']=='festival venue June 1'


def test_related_claim_counts_do_not_finish_before_missing_fact_is_searched():
    queries=[];reviews=[]
    def search(query):
        queries.append(query)
        return [{'url':'https://primary.example/venue'}] if query=='festival exact venue' else [
            {'url':'https://one.example/date'},{'url':'https://two.example/date'}]
    def read(url):return {'url':url,'summary':('The festival venue is the Blue Hall beside the river.' if 'venue' in url
        else 'The festival date is June 1 and the festival date remains unchanged this year.')}
    def assess(ledger):
        has_venue=any('Blue Hall' in c.text for c in ledger.claims.values())
        reviews.append(has_venue)
        return {'status':'assessed','sufficient':has_venue,'missing':[] if has_venue else ['venue'],
                'query':None if has_venue else 'festival exact venue'}
    result=ResearchLoop('Find the festival date and venue',search=search,read=read,
        assess_coverage=assess,budget=Budget(coverage_target=1,validation_rounds=0,max_queries=4)).run()
    assert queries[-1]=='festival exact venue' and reviews==[False,True]
    assert result['evidence_sufficient'] and result['stop_reason']=='evidence_sufficient'
    assert result['open_questions']==[]


def test_coverage_unknown_and_checkpoint_preserve_missing_requirements():
    ledger=ResearchLedger('Find date and venue',budget=Budget(coverage_target=1))
    ledger.coverage_required=True
    for url in ['https://one.example','https://two.example']:
        s=ledger.add_source(url);ledger.add_claim('The festival date is June 1.',s.source_id)
    assert not ledger.evidence_sufficient
    ledger.coverage_assessment={'status':'assessed','sufficient':False,'missing':['venue'],'query':'festival venue'}
    ledger.open_questions=['venue']
    restored=ResearchLedger.from_checkpoint(ledger.checkpoint())
    assert not restored.evidence_sufficient and restored.open_questions==['venue']
    assert restored.coverage_assessment['query']=='festival venue'


def test_long_page_retains_late_requested_evidence_verbatim_and_records_omissions():
    from app.backend.research.passages import select_passages
    early='The file header contains byte offsets and page layout.\n'
    late='The journal_mode WAL setting persists after closing and reopening the database.'
    text=early+('Unrelated documentation paragraph.\n'*2500)+late
    result=select_passages(text,'Describe file header offsets and whether journal_mode WAL persists after reopening.')
    assert early.strip() in result['summary'] and late in result['summary']
    assert len(result['summary'])<=20000 and result['truncated']
    assert any(span['start']>20000 for span in result['selected_ranges'])
    for span in result['selected_ranges']:
        assert text[span['start']:span['end']] in result['summary']


def test_an_answer_with_valid_citation_does_not_gain_unrelated_source_footer():
    from app.backend.chat.runners import _attach_validated_citations
    findings=[{'text':'Supported finding.','evidence':[{'url':'https://one.example/a','title':'One','validation':'validated'},
        {'url':'https://two.example/b','title':'Two','validation':'validated'}]}]
    answer='The supported answer is [here](https://one.example/a).'
    assert _attach_validated_citations(answer,findings)==answer


def test_table_column_values_do_not_run_together_or_lose_headers():
    from app.backend.tooling.web_page import PageText
    parser=PageText()
    parser.feed('<h2>Database header</h2><table><tr><th>Offset</th><th>Size</th><th>Description</th></tr>'
        '<tr><td>16</td><td>2</td><td>Database page size in bytes.</td></tr>'
        '<tr><td>20</td><td>1</td><td>Reserved space per page.</td></tr></table>')
    text=''.join(parser.parts)
    assert 'Offset: 16 | Size: 2 | Description: Database page size in bytes.' in text
    assert 'Offset: 20 | Size: 1 | Description: Reserved space per page.' in text
    assert 'Database header — Offset: 20' in text


def test_coverage_packet_preserves_less_repeated_question_part():
    from app.backend.chat.runners import LiveRunners
    ledger=ResearchLedger('Find SQLite header offsets and journal_mode WAL persistence after reopening')
    source=ledger.add_source('https://sqlite.org/docs')
    for n in range(24):
        ledger.add_claim(f'SQLite header offsets store database fields at position {n*3} and affect SQLite header offsets.',source.source_id)
    ledger.add_claim('The journal_mode WAL persistence remains after reopening the database.',source.source_id)
    captured=[]
    def generate(messages,**kwargs):
        packet=json.loads(messages[-1]['content']);captured.append(packet)
        assert any('persistence remains after reopening' in c['text'] for c in packet['evidence'])
        return json.dumps({'requirements':[{'need':'persistence','claims':[packet['evidence'][0]['id']]}],
            'missing':[],'query':None})
    runner=LiveRunners(generate_research_review=generate)
    runner._assess_coverage(ledger)
    assert len(captured[0]['evidence'])<=16


def test_disputed_only_support_does_not_close_a_requirement():
    value={'requirements':[{'need':'date','claims':['clm-1']}],'missing':[],'query':None}
    result=assess_coverage('Find date',[{'claim':'clm-1','text':'Conflicting date.','disputed':True}],lambda _:json.dumps(value))
    assert not result['sufficient'] and result['missing']


def test_markdown_documentation_is_read_without_browser_fallback(monkeypatch):
    import io
    from email.message import Message
    from types import SimpleNamespace
    from app.backend.tooling import web_page
    body=('# Dependency conditions\nservice_completed_successfully requires successful completion. '
          'restart: true excludes automatic container runtime restarts. '*4).encode()
    class Response(io.BytesIO):
        status=200
        headers=Message()
        headers['Content-Type']='text/markdown; charset=utf-8'
        def geturl(self):return 'https://docs.example/services'
    monkeypatch.setattr(web_page,'checked_public_url',lambda u:u)
    monkeypatch.setattr(web_page,'public_opener',lambda:SimpleNamespace(open=lambda *a,**k:Response(body)))
    page=web_page.read_public_page('https://docs.example/services',question='Dependency completion and restart')
    assert 'service_completed_successfully' in page['summary']
    assert 'automatic container runtime restarts' in page['summary']
    assert page['retrieval']=='public_http'


def test_valid_fourth_source_citation_is_not_removed_by_display_limit():
    from app.backend.chat.runners import _attach_validated_citations
    sources=[{'url':f'https://source{i}.example/page','title':f'Source {i}','validation':'validated'} for i in range(5)]
    findings=[{'text':f'Relevant fact {i}.','evidence':[s]} for i,s in enumerate(sources)]
    answer='The exception is documented [here](https://source4.example/page).'
    assert _attach_validated_citations(answer,findings,sources=sources)==answer


def test_one_mirror_cannot_fill_entire_multi_host_search_wave():
    urls=[*(f'https://mirror.example/doc{i}' for i in range(6)),'https://original.example/docs']
    reads=[]
    def read(url):
        reads.append(url)
        return {'url':url,'summary':'The requested specification describes persistence and file format behavior.'}
    ResearchLoop('Find the original specification',search=lambda _:[{'url':u} for u in urls],read=read,
        budget=Budget(max_queries=1,max_sources_per_query=5,max_sources=10,coverage_target=99)).run()
    assert 'https://original.example/docs' in reads
    assert sum('mirror.example' in u for u in reads)==3


def test_statistical_year_headers_in_data_cells_are_preserved():
    from app.backend.tooling.web_page import PageText
    p=PageText();p.feed('<h2>Report Viewer Configuration Error</h2><table>'
        '<tr><td>Park</td><td>2022</td><td>2023</td><td>2024</td></tr>'
        '<tr><td>Park A</td><td>100</td><td>150</td><td>200</td></tr></table>')
    text=''.join(p.parts)
    assert 'Park: Park A | 2022: 100 | 2023: 150 | 2024: 200' in text
    assert 'Configuration Error — Park' not in text


def test_html_source_line_wrapping_does_not_split_factual_sentences():
    from app.backend.tooling.web_page import PageText
    from app.backend.research.loop import statements_from_page
    p=PageText();p.feed('<h2>Persistence</h2><p>The WAL mode is persistent.\nIf you then close and\n'
        'reopen the database, the database will come back\nin WAL mode.</p>'
        '<pre>line one\nline two</pre>')
    text=''.join(p.parts)
    assert 'close and reopen the database' in text
    assert 'line one\nline two' in text
    claims=statements_from_page({'summary':text},question='Does WAL persist after reopening?')
    assert any('close and reopen' in c and 'WAL mode' in c for c in claims)


def test_markdown_rule_keeps_its_exception_and_subject_together():
    from app.backend.research.passages import markdown_blocks
    from app.backend.research.loop import statements_from_page
    text='- `restart`: Compose restarts the service after an update.\n  This excludes automatic runtime restarts\n  after a container dies.\n\n- `condition`: A separate rule.'
    page={'content_type':'text/markdown','summary':markdown_blocks(text)}
    claims=statements_from_page(page,question='What does restart exclude?')
    assert any('`restart`' in c and 'after a container dies' in c for c in claims)


def test_social_share_query_words_do_not_make_a_source_relevant():
    from app.backend.research.discovery import relevant_links
    page={'url':'https://blog.example/article','links':[
        {'url':'https://www.linkedin.com/shareArticle?title=SQLite+WAL+persistence','title':'Share'},
        {'url':'https://docs.example/spec?unrelated=SQLite+WAL+persistence','title':'Other'}]}
    assert relevant_links('SQLite WAL persistence',page)==[]


def test_empty_validation_wave_still_assesses_and_searches_missing_fact():
    searches=[]
    def search(q):
        searches.append(q)
        if q=='exact missing venue':return [{'url':'https://original.example/venue'}]
        return [{'url':'https://original.example/date'}]
    def read(url):return {'url':url,'summary':'The festival venue is Blue Hall on the river.' if url.endswith('venue')
        else 'The festival date is June 1 and the original event notice is available.'}
    def assess(ledger):
        done=any('Blue Hall' in c.text for c in ledger.claims.values())
        return {'status':'assessed','sufficient':done,'missing':[] if done else ['venue'],
                'query':None if done else 'exact missing venue'}
    report=ResearchLoop('Find festival date and venue',search=search,read=read,assess_coverage=assess,
        budget=Budget(max_queries=4,max_sources=5,validation_rounds=1)).run()
    assert 'exact missing venue' in searches and report['evidence_sufficient']


def test_original_source_request_prefers_observed_named_host_over_mirrors():
    read=[]
    def fetch(url):
        read.append(url)
        return {'url':url,'summary':'Acme format specification describes the requested byte fields clearly.'}
    ResearchLoop('Find original Acme documentation for format fields',
        search=lambda _:[{'url':'https://mirror.example/acme'},{'url':'https://docs.acme.org/format'}],
        read=fetch,budget=Budget(max_sources=1,max_queries=1)).run()
    assert read==['https://docs.acme.org/format']


def test_citation_with_parentheses_in_report_url_remains_whole():
    from app.backend.chat.runners import _attach_validated_citations
    url='https://stats.example/report?name=Visits+(1979+-+Present)&year=2024'
    findings=[{'text':'The report gives annual visits.','evidence':[{'url':url,'title':'Visits','validation':'validated'}]}]
    answer=f'The counts are in [the report]({url}).'
    assert _attach_validated_citations(answer,findings)==answer


def test_related_numeric_fields_are_not_fabricated_conflicts():
    ledger=ResearchLedger('SQLite usable page size')
    a=ledger.add_source('https://sqlite.org/format');b=ledger.add_source('https://mirror.example/format')
    row=ledger.add_claim('Offset 20 contains 1 byte for reserved space at the end of each page.',a.source_id)
    formula=ledger.add_claim('Usable page size is the page size at offset 16 less reserved space at offset 20.',b.source_id)
    assert not row.disputed and not formula.disputed
    left=ledger.add_claim('The tower is 324 metres tall.',a.source_id)
    right=ledger.add_claim('The tower is 330 metres tall.',b.source_id)
    assert left.disputed and right.disputed


def test_unchanged_evidence_is_not_reviewed_again_after_repeated_results():
    calls=[]
    def assess(ledger):
        calls.append(len(ledger.claims))
        return {'status':'assessed','sufficient':False,'missing':['venue'],'query':'different search'}
    ResearchLoop('festival date and venue',search=lambda _:[{'url':'https://original.example/date'}],
        read=lambda u:{'url':u,'summary':'The festival date is June 1 and its venue is not specified.'},
        assess_coverage=assess,budget=Budget(max_queries=3,validation_rounds=0)).run()
    assert calls==[1]


def test_gateway_error_page_is_not_validated_as_a_source():
    from app.backend.research.loop import validate_page
    with pytest.raises(ValueError,match='invalid page'):
        validate_page({'url':'https://stats.example','title':'504 Gateway Time-out',
            'summary':'504 Gateway Time-out. The upstream server did not respond.'},'https://stats.example')
