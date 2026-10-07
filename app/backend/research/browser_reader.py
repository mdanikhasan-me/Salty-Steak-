"""Read a browser source in bounded pages, retaining links and truncation state."""
from __future__ import annotations
from collections.abc import Callable, Mapping
import time
from urllib.parse import urljoin, urlsplit

def read_browser_source(call: Callable, address: str, *, question: str='', max_characters: int=24000,
                        max_pages: int=8) -> dict:
    # Scan beyond the answer-packet limit before selecting passages. Otherwise
    # a relevant paragraph past the first 24K characters can never be found.
    scan_limit=max_characters*4 if question.strip() else max_characters
    opened=call('open_url',url=address)
    if opened.get('status') != 'succeeded':
        raise RuntimeError(f"Browser navigation failed: {opened.get('error') or opened.get('status')}")
    tab=opened.get('tab')
    parts=[];table_rows=[];links=[];forms=[];seen=set();offset=0;text_offset=0;table_offset=0;page={};truncated=True
    render_wait_seconds=0.0
    for _ in range(max_pages):
        page=call('read_page',**({'tab':tab} if tab else {}),
            text_offset=text_offset,offset=offset,table_offset=table_offset,text_limit=max(1,min(8000,scan_limit-sum(map(len,parts)))),limit=40)
        if page.get('status')!='succeeded':
            raise RuntimeError(f"Browser page read failed: {page.get('error') or page.get('status')}")
        if not parts and page.get('text_length') is not None and int(page.get('text_length') or 0)<120:
            started=time.monotonic()
            # DOMContentLoaded can precede delayed client rendering. Wait only
            # for sparse initial reads, on the same tab, within a fixed budget.
            for retry in range(30):
                if time.monotonic()-started>=15 or int(page.get('text_length') or 0)>=120:
                    break
                time.sleep(.5)
                page=call('read_page',**({'tab':tab} if tab else {}),
                    text_offset=0,offset=0,table_offset=0,text_limit=min(8000,scan_limit),limit=40)
                if page.get('status')!='succeeded':
                    raise RuntimeError(f"Browser page read failed: {page.get('error') or page.get('status')}")
            render_wait_seconds=time.monotonic()-started
        final=str(page.get('url') or address)
        if parts and final != result_url:
            raise RuntimeError('Page navigated during paginated reading; mixed evidence discarded')
        result_url=final
        if not forms:forms=list(page.get('search_forms') or [])
        parts.append(str(page.get('summary') or page.get('text') or ''))
        table_rows.extend(str(row) for row in page.get('table_rows') or [])
        for control in page.get('controls') or []:
            if not isinstance(control,Mapping):continue
            href=urljoin(final,str(control.get('href') or '')) if control.get('href') else ''
            if urlsplit(href).scheme not in {'http','https'} or href in seen:continue
            seen.add(href);links.append({'url':href,'title':str(control.get('name') or '')[:240]})
        next_text=page.get('next_text_offset');next_controls=page.get('next_offset')
        next_table=page.get('next_table_offset')
        if next_text is None and next_controls is None and next_table is None:
            truncated=False;break
        if sum(map(len,parts))>=scan_limit and next_table is None:break
        new_text=int(next_text) if next_text is not None else int(page.get('text_length') or text_offset)
        new_controls=int(next_controls) if next_controls is not None else int(page.get('total_control_count') or offset)
        new_table=int(next_table) if next_table is not None else int(page.get('total_table_rows') or table_offset)
        if new_text<=text_offset and new_controls<=offset and new_table<=table_offset:break
        text_offset,offset=new_text,new_controls
        table_offset=new_table
    from .passages import select_passages
    # read_page slices one continuous innerText stream; inserting separators at
    # arbitrary offsets corrupts words, numbers and sentences across chunks.
    scanned_text='\n'.join([*table_rows,''.join(parts)])
    selected=select_passages(scanned_text,question,limit=max_characters)
    text=selected['summary']
    retained_rows=[row for row in table_rows if row in text]
    return {'url':str(page.get('url') or address),'title':str(page.get('title') or ''),
        'summary':text,'links':links,'search_forms':forms,'retrieval':'owned_browser_paginated',
        'content_characters':len(text),'truncated':truncated or selected['truncated'],
        'table_rows':retained_rows,'table_rows_read':len(retained_rows),
        'selected_ranges':selected['selected_ranges'],
        'selection':selected.get('selection','full_read'),
        'source_text_characters':len(scanned_text),
        'page_text_characters':int(page.get('text_length') or sum(map(len,parts))),
        'scanned_text_characters':sum(map(len,parts)),
        'render_wait_seconds':round(render_wait_seconds,3),
        'render_incomplete':bool(page.get('text_length') is not None and int(page.get('text_length') or 0)<120),
        'read_limit':{'max_characters':max_characters,'max_scan_characters':scan_limit,
                      'max_pages':max_pages},'pages_read':len(parts)}
