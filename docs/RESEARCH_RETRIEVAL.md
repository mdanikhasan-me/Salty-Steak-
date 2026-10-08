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

An optional coverage-model timeout or transport error leaves coverage explicitly
unavailable rather than discarding successfully retrieved evidence. The answering
pass retains those sources and the unknown-coverage status. User cancellation is not
converted into a successful response.

Saved answer provenance contains the same selected claims that were sent to the
answering pass. It is not independently reranked afterward: doing that can hide
primary evidence that actually supported the answer, especially after coverage review.

For original-source requests, matching a query word in a hostname does not establish
authority. Only explicitly supplied source URLs receive that direct-source preference.
The coverage packet also preserves a bounded spread of relevant claims across observed
sources so an early keyword-matching site cannot crowd out a later specification. Source
authority still needs evidence review; this sampling alone does not label a page primary.

For complete static HTML/Markdown/text reads, a bounded content sketch is computed
before question-specific passages are selected. It retains the lowest 256 fixed
hashes of seven-word sequences (no random seed). Long near-duplicate documents share
a document group, even on different hosts. Independence counts group contributing
sources by publisher or copied document, while preserving every original URL/claim.
Unrelated original reporting from a host that also carries a mirror stays separate.
The sketch is persisted in checkpoints, not repeated in answer-facing claim packets.

This is conservative copy detection, not proof of common authorship: at least 400
words, 128 distinct sampled sequences and estimated Jaccard overlap of 0.85 are
required. Shared short facts and repetitive boilerplate do not qualify. Translations,
heavily edited copies and partial browser/PDF reads can remain undetected; documents
without sketches retain the existing publisher-only heuristic.

## Acceptance boundaries and open work

Live extraction/source checks cover original RFC, Python, Docker and NASA pages.
The separate research acceptance reports include actual-model answers; reader-only
runs must not be represented as new complete model passes. Regression coverage includes
hidden contradictory facts, nested template tables, Markdown code/example links,
balanced destinations and provenance round trips.

Remaining limits include non-rendered CSS visibility, image-only PDF OCR, access
challenges and broad unseen-host coverage. The live RFC Editor/HTTPWG mirror case now
retains both URLs but counts one contributing origin; unidentified copies can still
inflate the heuristic count. Do not treat that heuristic as proof of independent
corroboration. Fresh full-model acceptance
and installed native release gates remain separate from source/test success.
