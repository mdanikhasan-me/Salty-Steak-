import socket
import pytest
from app.backend.tooling.web_page import PageText, checked_public_url

def test_public_reader_removes_executable_and_style_content():
    p=PageText();p.feed('<title>Source</title><style>hidden</style><script>bad()</script><h1>Evidence</h1><p>First &amp; second.</p>')
    assert ''.join(p.title)=='Source'
    assert 'hidden' not in ''.join(p.parts) and 'bad()' not in ''.join(p.parts)
    assert 'First & second.' in ''.join(p.parts)

@pytest.mark.parametrize('url',['file:///secret','https://user:pass@example.com','http://127.0.0.1/','http://192.168.0.1/','http://[::1]/','https://example.com:8443/'])
def test_public_reader_rejects_nonpublic_destinations(url):
    with pytest.raises(ValueError): checked_public_url(url)

def test_public_reader_rejects_hostname_resolving_to_private_address(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('10.0.0.1',80))])
    with pytest.raises(ValueError,match='private'):checked_public_url('https://example.com')

def test_research_search_failure_is_reported_instead_of_empty_evidence():
    from app.backend.research import ResearchLoop
    def fail(query): raise RuntimeError('Search provider unavailable')
    report=ResearchLoop('Explain event loops',search=fail,read=lambda url:{}).run()
    assert report['stop_reason']=='search_unavailable'
    assert report['waves'][0]['state']=='failed'
    assert report['waves'][0]['error']=='Search provider unavailable'

def test_last_allowed_query_still_reads_its_results():
    from app.backend.research import ResearchLoop, Budget
    reads=[]
    def read(url):
        reads.append(url)
        return {'url':url,'title':'Event loops','summary':'An event loop schedules asynchronous tasks without blocking other tasks. '*10}
    report=ResearchLoop('How event loops schedule tasks',search=lambda q:[{'url':'https://a.example/article','title':'Event loops'}],read=read,budget=Budget(max_queries=1,max_sources=2)).run()
    assert reads==['https://a.example/article']
    assert report['source_count']==1

def test_connected_peer_is_checked_again_before_sending_http():
    from types import SimpleNamespace
    from app.backend.tooling.web_page import verify_connected_peer
    closed=[]
    connection=SimpleNamespace(sock=SimpleNamespace(getpeername=lambda:('127.0.0.1',80)),close=lambda:closed.append(True))
    with pytest.raises(ValueError,match='private'):verify_connected_peer(connection)
    assert closed==[True]
