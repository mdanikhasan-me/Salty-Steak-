import pytest
from app.backend.research.discovery import relevant_links
from app.backend.research.browser_reader import read_browser_source
from app.backend.research.loop import ResearchLoop
from app.backend.research.ledger import Budget

def test_two_hop_html_record_is_read_before_search_tail_fills_budget():
    root='https://archive.example/catalogue'
    year='https://archive.example/zephyr/1964'
    leaf='https://archive.example/zephyr/1964/errata'
    pages={root:{'url':root,'summary':'Zephyr observatory catalogue containing historical observation records.',
                 'links':[{'url':year,'title':'Zephyr 1964 records'}]},
        year:{'url':year,'summary':'Zephyr observatory 1964 volume and its original errata record are linked below.',
              'links':[{'url':leaf,'title':'Zephyr 1964 errata'}]},
        leaf:{'url':leaf,'summary':'Zephyr 1964 errata: the instrument correction is precisely 37 units.'}}
    read=[]
    def fetch(url):read.append(url);return pages[url]
    loop=ResearchLoop('Find the Zephyr 1964 errata correction',
        search=lambda _:[{'url':root},*({'url':f'https://other.example/{i}'} for i in range(8))],
        read=fetch,budget=Budget(max_sources=3,max_queries=1,max_sources_per_query=3,coverage_target=99))
    report=loop.run()
    assert read==[root,year,leaf]
    assert any('37 units' in claim['text'] for claim in report['claims'])
    source=next(s for s in report['sources'] if s['url']==leaf)
    assert source['retrieval_details']['discovery_depth']==2
    assert source['retrieval_details']['discovered_from']==year

def test_discovery_is_bounded_and_does_not_follow_account_actions_or_code():
    page={'url':'https://archive.example/catalogue','links':[
        {'url':'/login','title':'Zephyr records'}, {'url':'javascript:alert(1)','title':'Zephyr records'},
        {'url':'/zephyr.html','title':'Zephyr records'}, {'url':'/zephyr.pdf','title':'Zephyr records'},
        {'url':'/zephyr.exe','title':'Zephyr records'}, {'url':'https://elsewhere.example/x','title':'Unrelated'}]}
    links=relevant_links('Zephyr observations',page)
    assert {x['url'] for x in links}=={'https://archive.example/zephyr.html','https://archive.example/zephyr.pdf'}
    assert relevant_links('Zephyr observations',page,depth=2)==[]
    assert relevant_links('Zephyr observations',page,attempted=[x['url'] for x in links])==[]

def test_browser_reader_retains_late_text_and_links_and_binds_tab():
    calls=[]
    def call(command,**args):
        calls.append((command,args))
        if command=='open_url':return {'status':'succeeded','tab':'tab-9'}
        assert args['tab']=='tab-9'
        if args['text_offset']==0:return {'status':'succeeded','url':'https://archive.example/a',
            'summary':'x'*8000,'text_length':8015,'next_text_offset':8000,'total_control_count':41,
            'next_offset':40,'controls':[]}
        assert args['offset']==40
        return {'status':'succeeded','url':'https://archive.example/a','summary':'important value',
            'next_text_offset':None,'next_offset':None,'controls':[{'href':'/hidden-record','name':'Original record'}]}
    result=read_browser_source(call,'https://archive.example/a')
    assert result['summary'].endswith('important value') and not result['truncated']
    assert result['links'][0]['url']=='https://archive.example/hidden-record'
    assert len(calls)==3

def test_browser_reader_reports_bounds_and_rejects_mixed_navigation():
    count=0
    def call(command,**args):
        nonlocal count
        if command=='open_url':return {'status':'succeeded','tab':'tab-1'}
        count+=1
        return {'status':'succeeded','url':f'https://archive.example/{count}', 'summary':'x'*8000,
            'next_text_offset':count*8000,'next_offset':None}
    with pytest.raises(RuntimeError,match='navigated'):read_browser_source(call,'https://archive.example/1')
    count=0
    result=read_browser_source(call,'https://archive.example/1',max_pages=1)
    assert result['truncated']


def test_checkpoint_retains_discovered_candidates_without_requery(tmp_path):
    from app.backend.research.loop import CHECKPOINT_SCHEMA
    import json
    ledger=ResearchLoop('Zephyr correction',search=lambda _:[],read=lambda _:{}).ledger
    ledger.record_query('Zephyr correction')
    checkpoint=tmp_path/'mission.json'
    checkpoint.write_text(json.dumps({'schema':CHECKPOINT_SCHEMA,'completed':False,
        'ledger':ledger.checkpoint(),'next_query':'Zephyr correction',
        'waves':[{'query':'Zephyr correction','state':'reading','sites':[],
                  'opened':0,'verified':0,'offers_verified':0}],
        'pending_candidates':[{'url':'https://archive.example/zephyr-correction','title':'Zephyr correction',
            '_discovery_depth':1,'_discovered_from':'https://archive.example/catalogue'}]}))
    reads=[]
    def no_search(_):raise AssertionError('Resume must keep the discovered record')
    def read(url):
        reads.append(url)
        return {'url':url,'summary':'Zephyr correction is 37 units according to the original record.'}
    loop=ResearchLoop('Zephyr correction',search=no_search,read=read,checkpoint_path=checkpoint)
    loop.ledger.budget.max_sources=1
    report=loop.run()
    assert reads==['https://archive.example/zephyr-correction']
    assert report['source_count']==1


def test_catalogue_identifier_stays_bound_to_its_short_description():
    from app.backend.research.loop import statements_from_page
    from app.backend.research.query import focused_search_query
    question='What item is described by reference Da/Mu 2/33a in the Darlington museum catalogue?'
    page={'summary':'Navigation details '*200+'\nRef: Da/Mu 2/33\nUnrelated prior object\n'
        'Ref: Da/Mu 2/33a\nDarlington Museum Photograph of a dead Polar Bear\n'
        'Ref: Da/Mu 2/34\nA different museum record'}
    found=statements_from_page(page,question=question)
    assert any('Da/Mu 2/33a' in s and 'Polar Bear' in s for s in found)
    query=focused_search_query(question)
    assert 'Da/Mu' in query and '2/33a' in query


def test_dynamic_public_shell_uses_paginated_browser(monkeypatch):
    from types import SimpleNamespace
    from app.backend.chat.service import ChatService
    from app.backend.tooling import web_page
    calls=[]
    monkeypatch.setattr(web_page,'read_public_page',lambda _: {'render_required':True,'summary':'Enable JavaScript'})
    monkeypatch.setattr(web_page,'checked_public_url',lambda url:url)
    class Broker:
        def invoke(self,request):
            calls.append(request['arguments'])
            return {'status':'succeeded','tab':'tab-7','url':'https://archive.example/data',
                    'summary':'Rendered record with the actual requested information.',
                    'next_offset':None,'next_text_offset':None}
    service=SimpleNamespace(automation=Broker(),granted_automation_capabilities=lambda:['browser.control'])
    result=ChatService._read_source(service,'https://archive.example/data')
    assert result['retrieval']=='owned_browser_paginated'
    assert [c['command'] for c in calls]==['open_url','read_page']


def test_public_refusal_does_not_fall_back_to_private_browser(monkeypatch):
    from types import SimpleNamespace
    from app.backend.chat.service import ChatService
    from app.backend.tooling import web_page
    def denied(_):raise ValueError('private address')
    monkeypatch.setattr(web_page,'read_public_page',denied)
    monkeypatch.setattr(web_page,'checked_public_url',denied)
    class Broker:
        def invoke(self,*args):raise AssertionError('Private source must not reach browser')
    service=SimpleNamespace(automation=Broker(),granted_automation_capabilities=lambda:['browser.control'])
    with pytest.raises(ValueError,match='private'):
        ChatService._read_source(service,'http://127.0.0.1/private')


def test_empty_first_search_tries_exact_reference_instead_of_giving_up():
    queries=[]
    def search(q):
        queries.append(q)
        return [{'url':'https://archive.example/record'}] if q=='"Ab/Cd 4/18"' else []
    loop=ResearchLoop('Find reference Ab/Cd 4/18 in the museum catalogue',search=search,
        read=lambda url:{'url':url,'summary':'Reference Ab/Cd 4/18 describes an original museum calibration notebook.'},
        budget=Budget(max_queries=3,max_sources=1,validation_rounds=0))
    report=loop.run()
    assert queries[1]=='"Ab/Cd 4/18"'
    assert report['source_count']==1


def test_repeated_host_timeouts_do_not_consume_all_page_attempts():
    reads=[]
    def read(url):
        reads.append(url)
        if 'dead.example' in url:raise TimeoutError('timed out')
        return {'url':url,'summary':'The original museum instrument calibration record contains the requested values.'}
    urls=[*(f'https://dead.example/{i}' for i in range(5)),'https://live.example/record']
    result=ResearchLoop('museum instrument calibration',search=lambda q:[{'url':u} for u in urls],read=read,
        budget=Budget(max_queries=1,max_sources=1)).run()
    assert len([u for u in reads if 'dead.example' in u])==2
    assert result['source_count']==1


def test_topic_coverage_without_requested_reference_cannot_finish_research():
    from app.backend.research.ledger import ResearchLedger
    ledger=ResearchLedger('Find the museum record Ab/Cd 4/18',budget=Budget(coverage_target=1))
    for url in ['https://museum.example/one','https://archive.example/two']:
        source=ledger.add_source(url)
        ledger.add_claim('The museum record collection includes historical photographs and notebooks.',source.source_id)
    assert not ledger.evidence_sufficient


def test_observed_get_catalogue_form_exposes_retrieval_url_without_site_specific_rules():
    from app.backend.tooling.web_page import PageText
    from app.backend.research.discovery import catalogue_search_links
    p=PageText();p.feed('<form action="/catalogue/search" method="get"><input name="Keyword">'
        '<input type="hidden" name="scope" value="museum"><select name="limit"><option value="50">50</option>'
        '<option value="100" selected>100</option></select><input type="submit" value="Search"></form>')
    page={'url':'https://archive.example/catalogue','search_forms':p.forms}
    result=catalogue_search_links('Find Ab/Cd 4/18',page)
    assert result[0]['url']=='https://archive.example/catalogue/search?scope=museum&limit=100&Keyword=Ab%2FCd'
    p.forms[0]['method']='post';assert not catalogue_search_links('Find Ab/Cd 4/18',page)
    p.forms[0]['method']='get';p.forms[0]['action']='https://another.example/search'
    assert not catalogue_search_links('Find Ab/Cd 4/18',page)


def test_catalogue_form_is_preferred_to_general_site_search():
    from app.backend.research.discovery import catalogue_search_links
    page={'url':'https://archive.example/catalogue','search_forms':[
        {'action':'/','method':'get','inputs':[{'name':'s','type':'search'}]},
        {'action':'','method':'get','inputs':[{'name':'tbKeywordCatalogue','type':'text'},
            {'name':'submitCatalogue','type':'submit','value':'Search'}]}]}
    links=catalogue_search_links('Find Ab/Cd 4/18',page)
    assert links[0]['_site_search']=={'term':'Ab/Cd','field':'tbKeywordCatalogue','submit':'submitCatalogue'}


def test_loop_can_search_catalogue_then_read_record_within_same_budget():
    root='https://archive.example/catalogue';result='https://archive.example/search?q=Ab%2FCd';leaf='https://archive.example/record'
    page={'url':root,'summary':'Public archive catalogue containing museum records and photographs.',
        'search_forms':[{'method':'get','action':'','inputs':[{'name':'Keyword','type':'text'},
            {'name':'submit','type':'submit','value':'Search'}]}]}
    calls=[]
    def search_site(url,form):
        calls.append((url,form))
        return {'url':result,'summary':'Results for Ab/Cd collection include the museum reference requested.',
            'links':[{'url':leaf,'title':'Ab/Cd 4/18 museum record'}]}
    def read(url):
        return page if url==root else {'url':leaf,'summary':'Ref: Ab/Cd 4/18\nMuseum observation notebook with original instrument measurements.'}
    report=ResearchLoop('Find museum reference Ab/Cd 4/18',search=lambda _:[{'url':root}],
        read=read,search_site=search_site,budget=Budget(max_queries=1,max_sources=3,max_sources_per_query=3)).run()
    assert len(calls)==1
    assert any('Ab/Cd 4/18' in c['text'] and 'notebook' in c['text'] for c in report['claims'])
