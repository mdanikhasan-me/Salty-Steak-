"""Source-link discovery from Markdown syntax, without rendering or executing it."""
from urllib.parse import urljoin, urlsplit

from markdown_it import MarkdownIt


def markdown_links(document: str, base_url: str, *, limit: int = 200) -> list[dict]:
    # A real parser handles balanced destinations, references and code spans.
    # Build per read: parser state/config is never shared between request threads.
    parser = MarkdownIt('commonmark')
    found, seen = [], set()
    for block in parser.parse(document):
        if block.type != 'inline':
            continue
        active, label = None, []
        for token in block.children or []:
            if token.type == 'link_open':
                active, label = token.attrGet('href'), []
            elif token.type == 'link_close' and active is not None:
                try:
                    address = urljoin(base_url, active)
                    parsed = urlsplit(address)
                    eligible = (not active.startswith('#') and parsed.scheme in {'http','https'}
                                and bool(parsed.hostname) and not parsed.username and not parsed.password)
                except ValueError:
                    eligible = False
                if eligible and address not in seen:
                    seen.add(address)
                    found.append({'url':address, 'title':' '.join(''.join(label).split())[:240]})
                    if len(found) >= limit:
                        return found
                active = None
            elif active is not None and token.type in {'text','code_inline','image'}:
                label.append(token.content)
            elif active is not None and token.type in {'softbreak','hardbreak'}:
                label.append(' ')
    return found
