"""Bounded, cookie-free retrieval of public source text for Research."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 2_000_000
PUBLIC_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
VOID_TAGS = frozenset({'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'})


def _explicitly_hidden(tag, attributes):
    if (tag in {'script','style','noscript','svg','template'} or 'hidden' in attributes
            or str(attributes.get('aria-hidden') or '').strip().lower() == 'true'):
        return True
    # Inline declarations only. External CSS/media queries require the browser
    # reader; this does not pretend to calculate the complete CSS cascade.
    style = re.sub(r'/\*.*?\*/', '', attributes.get('style') or '', flags=re.S)
    declarations = {}
    for declaration in style.split(';'):
        name, separator, value = declaration.partition(':')
        name, value = name.strip().lower(), value.strip().lower()
        if not separator or name not in {'display','visibility'}:
            continue
        important = bool(re.search(r'!\s*important\s*$', value))
        value = re.sub(r'!\s*important\s*$', '', value).strip()
        if name not in declarations or important or not declarations[name][1]:
            declarations[name] = (value, important)
    return (declarations.get('display', ('',False))[0] == 'none'
            or declarations.get('visibility', ('',False))[0] in {'hidden','collapse'})


class PDFReadError(ValueError):
    """A PDF could not be extracted; the browser plugin is not a text reader."""


def public_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirects(), PublicHTTPHandler(), PublicHTTPSHandler())


def bounded_body(response, limit: int, deadline: float) -> bytes:
    chunks, total = [], 0
    read = getattr(response, "read1", response.read)
    while total <= limit:
        if time.monotonic() >= deadline:
            raise TimeoutError("Source download exceeded its time budget")
        chunk = read(min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk); total += len(chunk)
    if total > limit:
        raise ValueError("Source exceeded the Research page size limit")
    return b"".join(chunks)


def checked_public_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Research sources must be public HTTP or HTTPS URLs without credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("Research source uses an unsupported port")
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Research cannot read local or private network addresses")
    return urllib.parse.urlunsplit(parsed._replace(fragment=""))

class PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, checked_public_url(newurl))

def verify_connected_peer(connection):
    if not ipaddress.ip_address(connection.sock.getpeername()[0]).is_global:
        connection.close()
        raise ValueError("Research connection resolved to a private address")

class PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect(); verify_connected_peer(self)

class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect(); verify_connected_peer(self)

class PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req): return self.do_open(PublicHTTPConnection, req)

class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req): return self.do_open(PublicHTTPSConnection, req, context=self._context)

class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self._visibility_stack = []
        self.hidden_elements = 0
        self.title_depth = 0
        self.title = []
        self.parts = []
        self.table_rows = []
        self.links = []
        self._link = None
        self.forms = []
        self._form = None
        self._select = None
        self._table_headers = []
        self._row = []
        self._cell = None
        self._cell_is_header = False
        self._heading = None
        self._last_heading = ''
        self._table_context = ''
        self._pre_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = _explicitly_hidden(tag, attributes)
        if hidden:
            self.hidden_elements += 1
        if tag not in VOID_TAGS:
            self._visibility_stack.append((tag, hidden))
            self.hidden += int(hidden)
        if self.hidden or hidden:
            return
        if tag=='pre':self._pre_depth+=1
        if tag in {'h1','h2','h3','h4','h5','h6'}:self._heading=[]
        if tag == 'table':self._table_headers=[];self._table_context=self._last_heading
        if tag == 'tr':self._row=[]
        if tag in {'td','th'}:
            self._cell=[];self._cell_is_header=tag=='th'
        if tag == 'form':
            self._form = {'action':attributes.get('action',''), 'method':attributes.get('method','get').lower(),
                          'inputs':[]}
        elif self._form is not None and tag == 'input':
            kind=attributes.get('type','text').lower()
            if kind not in {'checkbox','radio'} or 'checked' in attributes:
                self._form['inputs'].append({'name':attributes.get('name',''), 'type':kind,
                    'value':'' if kind=='password' or re.search(r'token|secret|password|csrf|session|authorization',attributes.get('name',''),re.I)
                        else attributes.get('value',''), 'label':attributes.get('aria-label') or attributes.get('placeholder','')})
        elif self._form is not None and tag == 'select':
            self._select={'name':attributes.get('name',''),'type':'select','value':'','has_option':False}
        elif self._select is not None and tag == 'option':
            if not self._select['has_option'] or 'selected' in attributes:
                self._select['value']=attributes.get('value','')
                self._select['has_option']=True
        if tag == "a":
            href = dict(attrs).get("href")
            self._link = {"url": href, "text": []} if href else None
        if tag == "title": self.title_depth += 1
        if self._cell is None and tag in {"p", "div", "br", "h1", "h2", "h3", "li", "tr", "article", "section"}: self.parts.append("\n")

    def handle_endtag(self, tag):
        was_hidden = self.hidden
        for index in range(len(self._visibility_stack)-1, -1, -1):
            if self._visibility_stack[index][0] == tag:
                self.hidden -= sum(int(hidden) for _,hidden in self._visibility_stack[index:])
                del self._visibility_stack[index:]
                break
        if was_hidden:
            return
        if tag=='pre':self._pre_depth=max(0,self._pre_depth-1)
        if tag in {'h1','h2','h3','h4','h5','h6'} and self._heading is not None:
            self._last_heading=' '.join(''.join(self._heading).split())[:240];self._heading=None
        if tag in {'td','th'} and self._cell is not None:
            self._row.append((' '.join(''.join(self._cell).split()),self._cell_is_header));self._cell=None
        if tag=='tr' and self._row:
            values=[value for value,_ in self._row]
            year_header=sum(bool(re.fullmatch(r'(?:19|20)\d{2}',value)) for value in values)>=3
            if all(header for _,header in self._row) or (not self._table_headers and year_header):self._table_headers=values
            elif len(self._table_headers)==len(values):
                values=[f'{header}: {value}' for header,value in zip(self._table_headers,values)]
            prefix=(self._table_context+' — ' if self._table_context and self._table_headers
                    and not re.search(r'error|warning|unavailable',self._table_context,re.I)
                    and not all(h for _,h in self._row) else '')
            line = prefix+' | '.join(values)
            self.parts.extend(['\n',line,'\n'])
            if not all(header for _,header in self._row):
                self.table_rows.append(line)
            self._row=[]
        if tag == 'select' and self._select is not None:
            if self._form is not None:self._form['inputs'].append({k:v for k,v in self._select.items() if k!='has_option'})
            self._select=None
        if tag == 'form' and self._form is not None:
            self.forms.append(self._form);self._form=None
        if tag == "a" and self._link is not None:
            self.links.append({"url": self._link["url"], "title": " ".join("".join(self._link["text"]).split())[:240]})
            self._link = None
        if tag == "title": self.title_depth = max(0, self.title_depth - 1)
        if self._cell is None and tag in {"p", "div", "h1", "h2", "h3", "li", "tr"}: self.parts.append("\n")

    def handle_data(self, data):
        if not self._pre_depth:data=re.sub(r'\s+',' ',data)
        if self._heading is not None and not self.hidden:self._heading.append(data)
        if self._cell is not None and not self.hidden:self._cell.append(data)
        if self._link is not None and not self.hidden:
            self._link["text"].append(data)
        if self.title_depth and not self.hidden: self.title.append(data)
        elif not self.hidden and self._cell is None: self.parts.append(data)

def read_public_page(url: str, *, timeout: float = 8.0, question: str = "") -> dict:
    address = checked_public_url(url)
    if address.startswith("http://"):
        try:
            return _read_public_page("https://" + address[7:], timeout=timeout, question=question)
        except (OSError, urllib.error.URLError):
            pass
    return _read_public_page(address, timeout=timeout, question=question)


def _read_public_page(url: str, *, timeout: float = 8.0, question: str = "",
                      allow_pdf_ranges: bool = True, pdf_deadline: float | None = None) -> dict:
    address = checked_public_url(url)
    request = urllib.request.Request(address, headers={
        "User-Agent": PUBLIC_USER_AGENT,
        "Accept": "text/html,application/pdf,text/plain;q=0.9", "Accept-Encoding": "identity",
    })
    opener = public_opener()
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200: raise RuntimeError(f"Source returned HTTP {response.status}")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/x-markdown", "application/pdf"}:
            raise ValueError("Source needs another reader for this content type")
        limit = 16 * 1024 * 1024 if content_type == "application/pdf" else MAX_BYTES
        length = int(response.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("Source exceeded the Research page size limit")
        validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
        if content_type == 'application/pdf' and pdf_deadline is None:
            pdf_deadline = time.monotonic() + 180
        if (allow_pdf_ranges and content_type == 'application/pdf' and length >= 1_000_000
                and validator and response.headers.get('Accept-Ranges') == 'bytes'):
            final_url = response.geturl()
            response.close()
            from .pdf_text import read_remote_pdf
            try:
                result = read_remote_pdf(final_url, length, validator)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                # Some ordinary PDFs require nearly every object to be read.
                # Retry a fresh bounded full representation, never combine
                # partial ranges with bytes from a different representation.
                if time.monotonic() >= pdf_deadline:
                    raise PDFReadError(f"PDF retrieval budget exhausted: {error}") from error
                try:
                    page = _read_public_page(final_url, timeout=timeout, question=question,
                        allow_pdf_ranges=False, pdf_deadline=pdf_deadline)
                except (OSError, ValueError, subprocess.TimeoutExpired) as full_error:
                    raise PDFReadError(f"PDF range and full retrieval failed: {full_error}") from full_error
                return {**page, 'fallback_reason':f'Selective PDF retrieval failed: {error}'[:240]}
            text = "\n\n".join(f"[PDF page {item['page']}]\n{item['text']}" for item in result['pages'])
            return {**result, "url":final_url, "title":result['title'] or urllib.parse.urlsplit(address).path.rsplit('/',1)[-1],
                    "summary":text,"retrieval":"public_pdf_ranges","content_sha256":hashlib.sha256(text.encode()).hexdigest(),
                    "hash_scope":"extracted_text","content_characters":len(text),"content_type":content_type}
        else:
            payload = bounded_body(response, limit, pdf_deadline if content_type == 'application/pdf'
                                   else time.monotonic() + 45)
        if content_type == "application/pdf":
            from .pdf_text import read_pdf_text
            try:
                result = read_pdf_text(payload)
            except (ValueError, subprocess.TimeoutExpired) as error:
                raise PDFReadError(str(error)) from error
            text = "\n\n".join(f"[PDF page {item['page']}]\n{item['text']}" for item in result['pages'])
            return {**result, "url": response.geturl(), "title": result['title'] or urllib.parse.urlsplit(address).path.rsplit('/',1)[-1],
                    "summary": text, "retrieval": "public_pdf", "content_sha256": hashlib.sha256(payload).hexdigest(),
                    "hash_scope":"response_body_bytes", "content_characters": len(text), "content_type": content_type}
        text = payload.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        final_url = response.geturl()
    forms=[]
    visibility = None
    table_rows=[]
    if content_type in {"text/html", "application/xhtml+xml"}:
        parser = PageText(); parser.feed(text)
        title = " ".join("".join(parser.title).split())
        text = "\n".join(line for part in "".join(parser.parts).splitlines() if (line := " ".join(part.split())))
        links = [{**item,"url":urllib.parse.urljoin(final_url,item['url'])} for item in parser.links[:200]
                 if urllib.parse.urlsplit(urllib.parse.urljoin(final_url,item['url'])).scheme in {'http','https'}]
        forms=parser.forms[:8]
        table_rows=parser.table_rows
        visibility = {'method':'explicit_html_and_inline_styles',
                      'hidden_elements':parser.hidden_elements, 'computed_styles':False}
    else:
        title = urllib.parse.urlsplit(address).hostname
        links = []
        if content_type in {'text/markdown','text/x-markdown'}:
            from ..research.passages import markdown_blocks
            from ..research.markdown_links import markdown_links
            links = markdown_links(text, final_url)
            text=markdown_blocks(text)
    if len(text.strip()) < 120: raise ValueError("Source did not return enough readable text")
    needs_render = bool(re.search(
        r"(?:please\s+enable|you\s+(?:must|need\s+to)\s+enable|requires?)\s+javascript|"
        r"^\s*(?:loading(?:\.\.\.|…)?|please\s+wait)\s*$", text[:3000], re.I))
    from ..research.passages import select_passages
    selected=select_passages(text,question)
    # Only expose rows retained in the bounded evidence packet. Their column
    # bindings must survive the subsequent statement parser as one unit.
    retained_rows=[row for row in table_rows if row in selected['summary']]
    return {"url": final_url, "title": title, "content_type":content_type, **selected,
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "hash_scope":"response_body_bytes",
            "retrieval": "public_http", "content_characters": len(selected['summary']), "links": links,
            "read_limit":{"max_characters":20_000},
            "render_required":needs_render and len(text)<1000, "search_forms":forms,
            "table_rows":retained_rows,"table_rows_read":len(retained_rows),
            **({'visibility':visibility} if visibility is not None else {})}
