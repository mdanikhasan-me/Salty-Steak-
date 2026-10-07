import pytest
from app.backend.tooling.pdf_text import read_pdf_text
from app.backend.research.loop import statements_from_page
from app.backend.research.query import focused_search_query


def pdf_bytes(text):
    import textwrap
    lines=textwrap.wrap(text,75)
    stream=("BT /F1 12 Tf 50 700 Td "+" Tj 0 -16 Td ".join(f"({line})" for line in lines)+" Tj ET").encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    value=bytearray(b'%PDF-1.4\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(value));value.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(value);value.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:value.extend(f'{offset:010d} 00000 n \n'.encode())
    value.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode())
    return bytes(value)


def test_real_pdf_worker_extracts_text_and_page_identity():
    sentence='The Observatory instrument calibration required a precise correction to the recorded magnetic data for the annual report. '
    result=read_pdf_text(pdf_bytes(sentence*2))
    assert result['page_count']==1
    assert result['pages'][0]['page']==1
    assert 'instrument calibration' in result['pages'][0]['text']


def test_missing_text_layer_does_not_pretend_ocr_ran():
    with pytest.raises(ValueError,match='OCR is required'):
        read_pdf_text(pdf_bytes(''))


def test_multiline_pdf_rows_preserve_page_and_printed_label():
    page={'pages':[{'page':10,'printed_label':'x','text':
        'All values of H should be decreased by 10 gamma.\nAll values of Z should be decreased by 27 gamma.'}]}
    statements=statements_from_page(page,question='H and Z decrease')
    assert len(statements)==2
    assert all('[PDF page 10; printed label x]' in s for s in statements)


def test_search_query_preserves_subject_without_answer_instructions():
    query=focused_search_query('Search the public web for the errata in The Observatories Yearbook 1964. Give the original URL and printed page number. If you cannot retrieve it, say so rather than guessing.')
    assert 'Observatories Yearbook 1964' in query
    assert 'guessing' not in query and 'original' not in query and 'printed' not in query


def test_retrieval_follows_a_relevant_document_from_a_catalogue():
    from app.backend.research import ResearchLoop, Budget
    calls=[]
    def read(url):
        calls.append(url)
        if url.endswith('catalog'):
            return {'url':url,'title':'Observatory catalogue','summary':'Catalogue of observatory reports and scientific measurements for researchers.',
                    'links':[{'url':'https://archive.example/observatory-1964.pdf','title':'1964 yearbook'},
                             {'url':'https://archive.example/menu.pdf','title':'Menu'}]}
        return {'url':url,'title':'Observatory 1964','summary':'The observatory published magnetic instrument corrections in this annual volume.'}
    ResearchLoop('Find observatory 1964 yearbook',search=lambda _: [{'url':'https://archive.example/catalog'}],
        read=read,budget=Budget(max_queries=1,max_sources=3,max_sources_per_query=3)).run()
    assert calls==['https://archive.example/catalog','https://archive.example/observatory-1964.pdf']
