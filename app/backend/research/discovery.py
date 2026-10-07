"""Bounded link discovery from retrieved public sources."""
from __future__ import annotations
import re
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from .query import QUERY_NOISE, requested_identifiers, focused_search_query

def url_key(value: str) -> str:
    try:
        p=urlsplit(value)
        if p.scheme.casefold() not in {'http','https'} or not p.hostname or p.username or p.password:
            return ''
        return urlunsplit((p.scheme.casefold(),p.netloc.casefold(),p.path or '/',p.query,''))
    except ValueError:
        return ''

def relevant_links(question: str, page: Mapping, *, depth: int=0,
                   attempted: Sequence[str]=(), limit: int=3) -> list[dict]:
    """Rank source links, keeping depth/budget explicit and never executing text."""
    if depth >= 2:return []
    base=str(page.get('url') or '')
    wanted=set(re.findall(r'[^\W_]+',question.casefold()))-QUERY_NOISE
    seen={url_key(base),*(url_key(x) for x in attempted)}
    candidates=[]
    for link in page.get('links') or []:
        if not isinstance(link,Mapping):continue
        raw=str(link.get('url') or '')
        if raw.startswith('#'):continue
        address=url_key(urljoin(base,raw))
        if not address or address in seen:continue
        parsed=urlsplit(address)
        if re.search(r'\.(?:png|jpe?g|gif|svg|mp4|mp3|css|js|exe|zip)$',parsed.path,re.I):continue
        if re.search(r'(?:^|/)(?:login|logout|sign-in|signup|cart|checkout|account|passwordreset|sharearticle|sharer(?:\.php)?|intent)(?:/|$)',parsed.path,re.I):continue
        label=str(link.get('title') or link.get('name') or '')
        if re.fullmatch(r'home|contact(?: us)?|about(?: us)?|bookstore|shop|privacy(?: policy)?|terms(?: of use)?|accessibility|share|sign in',label.strip(),re.I):continue
        if not parsed.path.strip('/') and not parsed.query:continue
        words=set(re.findall(r'[^\W_]+',(label+' '+parsed.path+' '+parsed.fragment).casefold()))
        overlap=len(wanted & words)
        document=parsed.path.casefold().endswith('.pdf')
        # Same-site navigation can reveal unindexed HTML records. External
        # references require two terms unless the link names a PDF document.
        same_host=parsed.hostname==urlsplit(base).hostname
        if overlap < (1 if same_host or document else 2):continue
        seen.add(address)
        candidates.append((overlap+(1 if document else 0), {
            'url':address,'title':label[:240], '_discovery_depth':depth+1,
            '_discovered_from':base,
        }))
    candidates.sort(key=lambda item:item[0],reverse=True)
    return [item for _,item in candidates[:limit]]


def catalogue_search_links(question: str, page: Mapping, *, depth: int=0) -> list[dict]:
    """Follow only observed same-site GET search forms, using public query terms."""
    if depth>=2:return []
    base=str(page.get('url') or '')
    identifiers=requested_identifiers(question)
    term=identifiers[0].split()[0] if identifiers else focused_search_query(question)[:160]
    forms=sorted(page.get('search_forms') or [],key=lambda f:any('catalog' in str(i.get('name','')).casefold() for i in f.get('inputs') or []),reverse=True)
    for form in forms:
        if form.get('method','get').casefold()!='get':continue
        action=url_key(urljoin(base,str(form.get('action') or base)))
        if not action or urlsplit(action).hostname != urlsplit(base).hostname:continue
        fields=form.get('inputs') or []
        if any(f.get('type') in {'password','file'} for f in fields):continue
        keyword_fields=[f for f in fields if f.get('type') in {'text','search'} and
            re.search(r'keyword',str(f.get('name') or ''),re.I)]
        submits=[f for f in fields if f.get('type')=='submit' and
            str(f.get('value') or '').strip().casefold()=='search']
        if not form.get('action') and len(keyword_fields)==1 and len(submits)==1:
            return [{'url':base,'title':'Catalogue search: '+term,'_discovery_depth':depth+1,
                '_discovered_from':base,'_site_search':{'term':term,
                    'field':keyword_fields[0]['name'],'submit':submits[0]['name']}}]
        candidates=[f for f in fields if f.get('type') in {'text','search'}
            and re.fullmatch(r'q|s|query|search|searchterm|searchtext|keyword|keywords',str(f.get('name') or ''),re.I)]
        if len(candidates)!=1:continue
        field=candidates[0]
        if not (field.get('type')=='search' or re.search(r'search|catalog',action,re.I)):continue
        params=dict(parse_qsl(urlsplit(action).query,keep_blank_values=True))
        for item in fields:
            name=str(item.get('name') or '')
            if not name or re.search(r'password|token|secret|email',name,re.I):continue
            if item.get('type') in {'hidden','checkbox','radio','select'}:
                params[name]=str(item.get('value') or '')
        params[str(field['name'])]=term
        parts=urlsplit(action);address=urlunsplit(parts._replace(query=urlencode(params)))
        return [{'url':address,'title':'Catalogue search: '+term,'_discovery_depth':depth+1,
                 '_discovered_from':base,'_search_form':True}]
    return []
