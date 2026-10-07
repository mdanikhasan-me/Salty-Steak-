"""Select bounded verbatim passages from an already fetched long page."""
from __future__ import annotations
import math,re
from collections import Counter
from .query import QUERY_NOISE


def pdf_blocks(text: str) -> list[str]:
    """Rejoin wrapped PDF prose without joining headings or numeric rows."""
    blocks, current = [], []

    def flush():
        if current:
            blocks.append(' '.join(current))
            current.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        words = line.split()
        numeric = sum(bool(re.search(r'\d', word)) for word in words)
        structural = (bool(re.match(r'^\d+(?:\.\d+)*\s+[A-Z]', line))
                      or (len(words) <= 4 and not re.search(r'[,.;:!?]$', line))
                      or (len(words) >= 2 and numeric / len(words) >= 0.4)
                      or '\t' in raw or bool(re.search(r'\S\s{3,}\S', raw)))
        if structural:
            flush()
            blocks.append(line)
            continue
        current.append(line)
        if re.search(r'[.!?][\]"\')]*$', line):
            flush()
    flush()
    return blocks


def markdown_blocks(text: str) -> str:
    """Join soft-wrapped prose/list items while preserving structural breaks."""
    blocks=[];current=[];fence=False
    def flush():
        if current:blocks.append(' '.join(current));current.clear()
    for line in text.splitlines():
        stripped=line.strip()
        if stripped.startswith(('```','~~~')):
            flush();fence=not fence;blocks.append(line);continue
        if fence:blocks.append(line);continue
        structural=bool(re.match(r'^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\|)',stripped))
        if not stripped:flush();continue
        if structural:flush()
        current.append(stripped)
    flush()
    return '\n'.join(blocks)

def select_passages(text: str, question: str, *, limit: int=20000) -> dict:
    if len(text)<=limit or not question.strip():
        return {'summary':text[:limit],'truncated':len(text)>limit,
                'selected_ranges':[{'start':0,'end':min(len(text),limit)}]}
    question=re.sub(r'https?://\S+',' ',question,flags=re.I)
    wanted={word for word in re.findall(r'[\w/-]+',question.casefold())
            if len(word)>1 and word not in QUERY_NOISE}
    if not wanted:return select_passages(text,'',limit=limit)
    blocks=[]
    for start in range(0,len(text),1600):
        end=min(len(text),start+2200)
        # Keep line boundaries where feasible; cutting a negation or subject
        # off a retrieved statement can change its meaning.
        if start:
            boundary=text.rfind('\n',max(0,start-400),start)
            if boundary>=0:start=boundary+1
        boundary=text.find('\n',end,min(len(text),end+400))
        if boundary>=0:end=boundary
        terms=set(re.findall(r'[\w/-]+',text[start:end].casefold()))
        blocks.append((start,end,terms&wanted))
    frequency=Counter(term for _,_,terms in blocks for term in terms)
    ranked=sorted(blocks,key=lambda block:sum(1+math.log((len(blocks)+1)/(frequency[t]+1))
                    for t in block[2]),reverse=True)
    first_end=min(1800,limit)
    boundary=text.rfind('\n',max(0,first_end-400),first_end)
    if boundary>0:first_end=boundary
    spans=[(0,first_end)]
    def merge(values):
        merged=[]
        for start,end in sorted(values):
            if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:merged.append((start,end))
        return merged
    for start,end,terms in ranked:
        if not terms:continue
        merged=merge([*spans,(start,end)])
        if sum(b-a for a,b in merged)+2*(len(merged)-1)<=limit:spans=merged
    return {'summary':'\n\n'.join(text[a:b] for a,b in spans),'truncated':True,
        'selected_ranges':[{'start':a,'end':b} for a,b in spans],
        'selection':'question_relevant_verbatim_passages','source_text_characters':len(text)}
