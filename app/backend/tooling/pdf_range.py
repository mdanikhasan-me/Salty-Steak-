"""Seekable public PDF reader: fetch text objects without downloading scan images."""
from __future__ import annotations

import io
import json
import re
import time
import urllib.request
import urllib.error
import urllib.parse
import http.client

from app.backend.tooling.web_page import checked_public_url, bounded_body, PUBLIC_USER_AGENT, PublicHTTPSConnection, PublicHTTPConnection
from app.backend.tooling.pdf_text import printed_page_label


class PersistentRangeOpener:
    """Reuse one verified connection for same-document byte ranges."""
    def __init__(self):
        self.connection = None
        self.origin = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def open(self, request, timeout):
        parsed = urllib.parse.urlsplit(checked_public_url(request.full_url))
        origin=(parsed.scheme,parsed.hostname,parsed.port)
        if self.connection is None or self.origin!=origin:
            self.close()
            factory=PublicHTTPSConnection if parsed.scheme=='https' else PublicHTTPConnection
            self.connection=factory(parsed.hostname,port=parsed.port,timeout=timeout)
            self.origin=origin
        self.connection.timeout=timeout
        if self.connection.sock:self.connection.sock.settimeout(timeout)
        target=urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        try:
            self.connection.request('GET',target,headers={**dict(request.header_items()),'Connection':'keep-alive'})
            response=self.connection.getresponse()
            response.geturl=lambda:request.full_url
            return response
        except (OSError,http.client.HTTPException):
            self.close()
            raise


public_opener = PersistentRangeOpener


class RangeReader(io.RawIOBase):
    def __init__(self, url, length, validator, *, deadline, block_size=16384, max_requests=96):
        self.url=checked_public_url(url);self.length=length;self.validator=validator
        self.deadline=deadline;self.block_size=block_size;self.max_requests=max_requests
        self.position=0;self.cache={};self.requests=0;self.bytes_read=0
        self.failure = None
        self.opener=public_opener()

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def readinto(self, buffer):
        try:
            data=self.read(len(buffer))
        except Exception as error:
            self.failure = error
            raise
        buffer[:len(data)]=data
        return len(data)

    def seek(self, offset, whence=0):
        position=offset if whence==0 else self.position+offset if whence==1 else self.length+offset
        if position<0: raise ValueError('Negative PDF offset')
        self.position=position
        return position

    def read(self, size=-1):
        size=self.length-self.position if size<0 else min(size,self.length-self.position)
        if size<=0:return b''
        if size>2*1024*1024:raise ValueError('PDF object exceeded the range reader limit')
        output=[]
        while size:
            index=self.position//self.block_size
            if index not in self.cache:
                if time.monotonic()>=self.deadline or self.requests>=self.max_requests:
                    raise TimeoutError('PDF selective-reading budget exhausted')
                start=index*self.block_size;end=min(start+self.block_size,self.length)-1
                request=urllib.request.Request(self.url,headers={'User-Agent':PUBLIC_USER_AGENT,
                    'Accept-Encoding':'identity','Range':f'bytes={start}-{end}','If-Range':self.validator})
                for attempt in range(2):
                    self.requests+=1
                    try:
                        with self.opener.open(request,timeout=min(15,max(1,self.deadline-time.monotonic()))) as response:
                            if (response.status!=206 or response.headers.get('Content-Range')!=f'bytes {start}-{end}/{self.length}'
                                or response.geturl()!=self.url
                                or (response.headers.get('ETag') or response.headers.get('Last-Modified'))!=self.validator
                                or response.headers.get('Content-Encoding') not in (None,'identity')):
                                raise ValueError('PDF range identity or boundaries changed')
                            data=bounded_body(response,end-start+1,self.deadline)
                        break
                    except (OSError, urllib.error.URLError, http.client.HTTPException):
                        if hasattr(self.opener,'close'):self.opener.close()
                        if attempt or time.monotonic()>=self.deadline or self.requests>=self.max_requests:
                            raise
                if len(data)!=end-start+1:raise ValueError('Incomplete PDF range')
                self.cache[index]=data;self.bytes_read+=len(data)
            block=self.cache[index];offset=self.position%self.block_size;n=min(size,len(block)-offset)
            if n<=0:raise ValueError('Invalid PDF range offset')
            output.append(block[offset:offset+n]);self.position+=n;size-=n
        return b''.join(output)


def extract_remote_pdf(request):
    import pypdfium2 as pdfium
    stream=RangeReader(request['url'],int(request['length']),request['validator'],deadline=time.monotonic()+100,max_requests=128)
    pages=[];characters=0;stop=None
    try:
        with pdfium.PdfDocument(stream) as document:
            if stream.failure:raise stream.failure
            count=len(document)
            if count>2000:raise ValueError('PDF page count exceeds the selective reader limit')
            for index in range(min(count,200)):
                try:
                    if time.monotonic()>=stream.deadline:raise TimeoutError('PDF selective-reading budget exhausted')
                    page=document[index]
                    try:
                        textpage=page.get_textpage()
                        try:
                            text=textpage.get_text_bounded().strip()
                            if stream.failure:raise stream.failure
                        finally:textpage.close()
                    finally:page.close()
                except Exception as error:
                    if not pages:raise
                    stop=str(error);break
                if text:
                    pages.append({'page':index+1,'text':text[:500000-characters],
                                  'printed_label':printed_page_label(text)})
                    characters+=len(pages[-1]['text'])
                if characters>=500000:stop='Text limit';break
    except Exception as error:
        # PDFium catches exceptions raised by its byte-reading callback. Keep
        # the actual network/budget failure instead of its generic format error.
        if stream.failure is not None:
            raise stream.failure from error
        raise
    finally:
        if hasattr(stream.opener,'close'):stream.opener.close()
    if characters<120:raise ValueError('No usable text retrieved; image-only pages may require OCR')
    return {'title':'', 'pages':pages,'page_count':count,
            'truncated':bool(stop) or count>200,'read_limit':stop,'downloaded_bytes':stream.bytes_read,
            'range_requests':stream.requests,'document_bytes':stream.length,
            'extraction':'selective_pdf_text_layer; OCR errors may remain', 'representation_validator':stream.validator}


if __name__=='__main__':
    import sys
    try:
        result=extract_remote_pdf(json.load(sys.stdin))
        sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))
    except Exception as error:
        sys.stderr.write(str(error));raise SystemExit(1)
