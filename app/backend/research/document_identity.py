"""Bounded content sketches for detecting copied documents across hosts.

This is a conservative independence heuristic, not author attribution or truth.
It uses complete extracted text before question-specific passage selection.
"""
import hashlib
import heapq
import re

ALGORITHM = 'bottom256-word7-blake2b64-v1'
SKETCH_SIZE = 256


def document_fingerprint(text: str) -> dict:
    words = re.findall(r'\w+', text.casefold())
    retained, heap = set(), []
    for index in range(len(words)-6):
        value = int.from_bytes(hashlib.blake2b(
            ' '.join(words[index:index+7]).encode(), digest_size=8).digest(), 'big')
        if value in retained:
            continue
        if len(heap) < SKETCH_SIZE:
            heapq.heappush(heap, -value)
            retained.add(value)
        elif value < -heap[0]:
            removed = -heapq.heapreplace(heap, -value)
            retained.remove(removed)
            retained.add(value)
    return {'algorithm':ALGORITHM, 'word_count':len(words),
            'sketch':[f'{value:016x}' for value in sorted(retained)]}


def copied_document_similarity(left: dict, right: dict) -> float | None:
    if (left.get('algorithm') != ALGORITHM or right.get('algorithm') != ALGORITHM
            or min(left.get('word_count',0),right.get('word_count',0)) < 400):
        return None
    a, b = set(left.get('sketch',[])), set(right.get('sketch',[]))
    # Short/repetitive shared disclaimers must not establish document identity.
    if min(len(a),len(b)) < 128:
        return None
    sample = set(sorted(a | b)[:SKETCH_SIZE])
    return len(sample & a & b) / len(sample)
