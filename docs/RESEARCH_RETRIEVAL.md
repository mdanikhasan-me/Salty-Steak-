# Research retrieval contracts

The app's own pipeline is search/discovery → HTTP or owned-browser retrieval →
bounded extraction → claim ledger → coverage review → answer with source URLs.
Successful HTTP status alone does not establish correct extraction or answer quality.

## Source reading

Static HTML reading excludes explicit hidden subtrees, inline display/visibility
suppression, scripts, templates and other non-readable markup. Hidden links and
forms are excluded from discovery too; hidden tables cannot rewrite visible column
bindings. This is not computed CSS: external stylesheets, layout and client-side
rendering still require the owned-browser path.

Markdown source discovery uses the pinned CommonMark parser already present in the
runtime, not a destination regex. Balanced parentheses, reference links and autolinks
are supported. Code examples, images, fragment-only targets, duplicate destinations
and non-HTTP schemes are not treated as discovered source links. Content extraction
continues to preserve the original prose/table text instead of executing or rendering it.

## Provenance

Each validated source keeps its requested/final URL, retrieval method, limits,
selection ranges, truncation and applicable visibility metadata. The downloaded
representation hash is retained separately from the validated summary hash:

- `reader_content_sha256` / `reader_hash_scope`: reader representation (downloaded
  body bytes for HTTP/full PDF; extracted text for the selective PDF reader).
- `content_sha256` / `hash_scope`: exact validated summary, UTF-8 encoded.

These identify different representations and must not overwrite each other.
The claim ledger/checkpoint retains both scopes; neither hash proves factual truth.

## Acceptance boundaries and open work

Live extraction/source checks cover original RFC, Python, Docker and NASA pages.
The separate research acceptance reports include actual-model answers; reader-only
runs must not be represented as new complete model passes. Regression coverage includes
hidden contradictory facts, nested template tables, Markdown code/example links,
balanced destinations and provenance round trips.

Remaining limits include non-rendered CSS visibility, image-only PDF OCR, access
challenges and broad unseen-host coverage. Cross-host mirrors can still inflate the
ledger's heuristic independent-source count when origin identity is not established;
the live RFC run exposed this between the RFC Editor and HTTPWG copies. Do not treat
that heuristic as proof of independent corroboration. Fresh full-model acceptance
and installed native release gates remain separate from source/test success.
