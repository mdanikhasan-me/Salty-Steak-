import io
import time
from types import SimpleNamespace
import pytest
from app.backend.tooling import pdf_range


def install_opener(monkeypatch, payload, *, wrong_validator=False, incomplete=False):
    calls=[]
    class Response(io.BytesIO):
        status=206
        def __init__(self,start,end):
            data=payload[start:end+1]
            super().__init__(data[:-1] if incomplete else data)
            self.headers={'Content-Range':f'bytes {start}-{end}/{len(payload)}',
                          'ETag':'changed' if wrong_validator else 'stable'}
        def geturl(self):return 'https://archive.example/report.pdf'
    def open_request(request,timeout):
        value=request.get_header('Range').split('=')[1]
        start,end=map(int,value.split('-'));calls.append((start,end))
        return Response(start,end)
    monkeypatch.setattr(pdf_range,'checked_public_url',lambda u:u)
    monkeypatch.setattr(pdf_range,'public_opener',lambda:SimpleNamespace(open=open_request))
    return calls


def test_seek_and_read_fetch_only_needed_ranges_and_reuse_cache(monkeypatch):
    data=bytes(range(256))*8
    calls=install_opener(monkeypatch,data)
    r=pdf_range.RangeReader('https://archive.example/report.pdf',len(data),'stable',deadline=time.monotonic()+10,block_size=64)
    r.seek(-80,2);assert r.read(80)==data[-80:]
    r.seek(10);assert r.read(12)==data[10:22]
    count=len(calls);r.seek(11);assert r.read(2)==data[11:13]
    assert len(calls)==count and r.bytes_read<len(data)


@pytest.mark.parametrize('option',['wrong_validator','incomplete'])
def test_changed_or_incomplete_pdf_ranges_are_rejected(monkeypatch,option):
    install_opener(monkeypatch,b'x'*256,**{option:True})
    r=pdf_range.RangeReader('https://archive.example/report.pdf',256,'stable',deadline=time.monotonic()+10,block_size=64)
    with pytest.raises(ValueError):r.read(1)


def test_range_request_budget_stops_network_calls(monkeypatch):
    calls=install_opener(monkeypatch,b'x'*256)
    r=pdf_range.RangeReader('https://archive.example/report.pdf',256,'stable',deadline=time.monotonic()+10,block_size=64,max_requests=1)
    r.read(1);r.seek(128)
    with pytest.raises(TimeoutError):r.read(1)
    assert len(calls)==1


def test_pdfium_reads_the_seekable_network_stream(monkeypatch):
    from tests.test_pdf_retrieval import pdf_bytes
    payload=pdf_bytes('Instrument calibration measurements are recorded with the original dates, units and observations. '*3)
    install_opener(monkeypatch,payload)
    report=pdf_range.extract_remote_pdf({'url':'https://archive.example/report.pdf','length':len(payload),'validator':'stable'})
    assert report['page_count']==1
    assert 'Instrument calibration' in report['pages'][0]['text']
    assert report['range_requests']>=1
