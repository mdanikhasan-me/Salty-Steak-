"""Bounded, cookie-free retrieval of public source text for Research."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_BYTES = 2_000_000

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
        self.title_depth = 0
        self.title = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}: self.hidden += 1
        if tag == "title": self.title_depth += 1
        if tag in {"p", "div", "br", "h1", "h2", "h3", "li", "tr", "article", "section"}: self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"}: self.hidden = max(0, self.hidden - 1)
        if tag == "title": self.title_depth = max(0, self.title_depth - 1)
        if tag in {"p", "div", "h1", "h2", "h3", "li", "tr"}: self.parts.append("\n")

    def handle_data(self, data):
        if self.title_depth: self.title.append(data)
        elif not self.hidden: self.parts.append(data)

def read_public_page(url: str, *, timeout: float = 8.0) -> dict:
    address = checked_public_url(url)
    request = urllib.request.Request(address, headers={
        "User-Agent": "Mozilla/5.0 (compatible; SaltySteakResearch/1.0)",
        "Accept": "text/html,text/plain;q=0.9", "Accept-Encoding": "identity",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirects(), PublicHTTPHandler(), PublicHTTPSHandler())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200: raise RuntimeError(f"Source returned HTTP {response.status}")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ValueError("Source needs another reader for this content type")
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES: raise ValueError("Source exceeded the Research page size limit")
        text = payload.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        final_url = response.geturl()
    if content_type != "text/plain":
        parser = PageText(); parser.feed(text)
        title = " ".join("".join(parser.title).split())
        text = "\n".join(line for part in "".join(parser.parts).splitlines() if (line := " ".join(part.split())))
    else: title = urllib.parse.urlsplit(address).hostname
    if len(text.strip()) < 120: raise ValueError("Source did not return enough readable text")
    return {"url": final_url, "title": title, "summary": text[:20_000],
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "retrieval": "public_http", "content_characters": min(len(text), 20_000)}
