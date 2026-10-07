from app.backend.research.query import requested_urls, focused_search_query
from app.backend.research.loop import ResearchLoop
from app.backend.research.ledger import Budget

def test_supplied_urls_preserve_parentheses_and_queries_without_markdown_punctuation():
    q='Read [report](https://stats.example/report_(2024)?year=2024) and https://docs.example/a#part.'
    assert requested_urls(q)==['https://stats.example/report_(2024)?year=2024','https://docs.example/a']

def test_direct_source_can_answer_when_search_is_down():
    calls=[]
    def search(q):
        calls.append('search');raise RuntimeError('Index offline')
    def read(url):
        calls.append(url)
        return {'url':url,'summary':'Retry-After allows an HTTP-date or a delay in seconds.'}
    report=ResearchLoop('Read https://spec.example/http and explain Retry-After',search=search,read=read,
        budget=Budget(max_sources=1,max_queries=1,validation_rounds=0)).run()
    assert calls==['https://spec.example/http']
    assert report['source_count']==1 and report['queries']==[]
    assert report['waves'][0]['source_origin']=='user_urls'

def test_failed_supplied_source_still_searches_for_alternatives():
    calls=[]
    def read(url):
        calls.append(url)
        if url.endswith('missing'):raise ValueError('Not found')
        return {'url':url,'summary':'Retry-After allows an HTTP-date or a delay in seconds.'}
    def search(q):
        calls.append('search');assert 'https' not in q
        return [{'url':'https://spec.example/current'}]
    report=ResearchLoop('Read https://spec.example/missing and explain Retry-After',search=search,read=read,
        budget=Budget(max_queries=1,validation_rounds=0)).run()
    assert calls==['https://spec.example/missing','search','https://spec.example/current']
    assert report['source_count']==1 and len(report['rejected_sources'])==1

def test_direct_source_does_not_suppress_search_for_missing_requirements():
    calls=[]
    def read(url):return {'url':url,'summary':'The event date is June 1; the venue is Blue Hall.' if 'venue' in url
                          else 'The event date is June 1 according to the official notice.'}
    def search(q):calls.append(q);return [{'url':'https://event.example/venue'}]
    def assess(ledger):
        done=any('Blue Hall' in c.text for c in ledger.claims.values())
        return {'status':'assessed','sufficient':done,'missing':[] if done else ['venue'],'query':None if done else 'event venue'}
    report=ResearchLoop('Read https://event.example/date and find the event date and venue',search=search,read=read,
        assess_coverage=assess,budget=Budget(max_queries=2,validation_rounds=0,coverage_target=99)).run()
    assert calls==['event venue'] and report['evidence_sufficient']

def test_direct_source_checkpoint_does_not_reread_completed_source(tmp_path):
    checkpoint=tmp_path/'research.json'
    reads=[]
    def read(url):
        reads.append(url)
        return {'url':url,'summary':'The requested document contains a clear statement of the event date.'}
    first=ResearchLoop('Read https://event.example/date',search=lambda _:[],read=read,
        checkpoint_path=checkpoint,budget=Budget(max_sources=1,validation_rounds=0)).run()
    second=ResearchLoop('Read https://event.example/date',search=lambda _:[],read=read,
        checkpoint_path=checkpoint).run()
    assert reads==['https://event.example/date']
    assert second['source_count']==first['source_count']==1

def test_url_does_not_use_search_budget():
    reads=[]
    def read(url):
        reads.append(url)
        return {'url':url,'summary':'The first archive has the event date but no venue information.'}
    report=ResearchLoop('Read https://event.example/date and find the venue',
        search=lambda _:[{'url':'https://other.example/venue'}],read=read,
        budget=Budget(max_queries=1,validation_rounds=1)).run()
    assert len(report['queries'])==1 and len(reads)==2

def test_document_link_precedes_generic_search_form():
    reads=[]
    landing='https://paper.example/abstract'
    pdf='https://paper.example/transformer.pdf'
    def read(url):
        reads.append(url)
        if url==landing:
            return {'url':url,'summary':'The Transformer paper full text and abstract are available below.',
                    'links':[{'url':pdf,'title':'Transformer full paper PDF'}],
                    'search_forms':[{'action':'/search','method':'get','inputs':[{'name':'q','type':'search'}]}]}
        return {'url':url,'summary':'The Transformer architecture replaces recurrent layers with attention.'}
    ResearchLoop('Read https://paper.example/abstract and the Transformer paper PDF',search=lambda _:[],read=read,
        budget=Budget(max_sources=2,max_queries=1,validation_rounds=0)).run()
    assert reads==[landing,pdf]

def test_url_words_do_not_displace_the_requested_rule_from_review_packet():
    import json
    from app.backend.research.ledger import ResearchLedger
    from app.backend.chat.runners import LiveRunners
    url='https://www.rfc-editor.org/rfc/rfc9110.html'
    ledger=ResearchLedger(f'Read {url} and explain the two allowed forms of Retry-After and what it means with a 503 response. Cite the original specification.')
    source=ledger.add_source(url,'HTTP Semantics specification')
    for n in range(45):
        ledger.add_claim(f'Reference specification {n}: https://www.rfc-editor.org/info/rfc{1000+n} with editorial bibliography.',source.source_id)
    fact='The Retry-After field value can be either an HTTP-date or a number of seconds to delay after receiving the response.'
    ledger.add_claim(fact,source.source_id)
    captured=[]
    def generate(messages,**kwargs):
        payload=json.loads(messages[1]['content']);captured.append(payload)
        return '{}'
    LiveRunners(generate_research_review=generate)._assess_coverage(ledger)
    assert any(c['text']==fact for c in captured[0]['evidence'])
