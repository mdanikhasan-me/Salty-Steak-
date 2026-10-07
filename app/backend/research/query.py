"""Keep retrieval terms focused on the requested subject, not answer formatting."""
from __future__ import annotations
import re
from urllib.parse import urlsplit, urlunsplit


def requested_urls(question: str) -> list[str]:
    """Extract explicitly supplied HTTP sources; fetching still checks network safety."""
    found = []
    for match in re.finditer(r'https?://[^\s<>"\u201c\u201d]+', str(question or ''), re.I):
        value = match.group().rstrip('.,;:!?')
        while value.endswith(')') and value.count(')') > value.count('('):
            value = value[:-1]
        value = value.rstrip(']')
        try:
            parsed = urlsplit(value)
            if not parsed.hostname or parsed.username or parsed.password:
                continue
            value = urlunsplit(parsed._replace(fragment=''))
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    return found[:8]

QUERY_NOISE = frozenset('''a an the and or of to in on for is are was were be been it its
this that with from by as at i you me my please search research public web internet online
find look up fetch retrieve give provide answer tell explain what which where when why how
much must should would could can cannot do does did not if then rather than guessing say
separately exact original url urls link links cite citations printed page number numbers
sources source information'''.split())


def focused_search_query(question: str) -> str:
    text = str(question or '').strip()
    for url in requested_urls(text):
        text = text.replace(url, ' ')
    # Explicit search operators and quotes are useful index instructions.
    if re.search(r'\b(?:site|filetype|intitle):', text, re.I):
        return text[:500]
    text = re.split(r'\b(?:if you cannot|if you can.t|do not guess)\b', text, maxsplit=1, flags=re.I)[0]
    tokens = re.findall(r'[^\W_]+(?:[-/\u2019\'][^\W_]+)*', text)
    kept, seen = [], set()
    for token in tokens:
        key=token.casefold()
        if key in QUERY_NOISE or key in seen:
            continue
        seen.add(key);kept.append(token)
    return ' '.join(kept[:22])[:240] or text[:240]


def requested_identifiers(question: str) -> list[str]:
    return re.findall(r'\b[A-Za-z]{1,12}(?:/[A-Za-z]{1,12})+\s+\d+(?:/[\w.-]+)+', question)


def discovery_queries(question: str) -> list[str]:
    """Alternative index queries for exact identifiers and quoted names."""
    references=requested_identifiers(question)
    quoted=re.findall(r'"([^"\n]{3,100})"',question)
    queries=[]
    for identifier in [*references,*quoted][:3]:
        queries.append('"'+identifier+'"')
    for reference in references[:2]:
        collection=reference.split()[0]
        subject=focused_search_query(question.replace(reference,''))
        queries.append('"'+collection+'" '+subject[:180])
    parts=re.findall(r'([^?]+)\?',question)
    if len(parts)>1:
        anchor=' '.join(focused_search_query(parts[0]).split()[:3])
        for part in reversed(parts[1:]):
            queries.append((anchor+' '+focused_search_query(part)).strip()[:300])
    return list(dict.fromkeys(queries))
