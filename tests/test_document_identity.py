from app.backend.research.document_identity import document_fingerprint, copied_document_similarity
from app.backend.research.ledger import ResearchLedger, Budget


def document(subject='botanical'):
    return '\n'.join(f'The {subject} survey station number {i} recorded detailed measurements '
                     f'for sample {i+10} during the annual observation period.' for i in range(100))


def add(ledger, url, text):
    return ledger.add_source(url, document_fingerprint=document_fingerprint(text))


def test_content_sketch_is_bounded_and_format_insensitive():
    text = document()
    first = document_fingerprint(text)
    second = document_fingerprint(text.upper().replace(' ', '  ').replace('.', '¶'))
    assert first == second
    assert len(first['sketch']) == 256
    assert copied_document_similarity(first, second) == 1
    assert len(set(first['sketch'])) == 256


def test_short_shared_fact_and_repeated_boilerplate_do_not_establish_copy():
    for text in ('The tower is 324 metres tall.', 'Read our privacy policy. '*1000):
        fingerprint = document_fingerprint(text)
        assert copied_document_similarity(fingerprint,fingerprint) is None


def test_mirror_cannot_satisfy_independent_source_gate_but_keeps_both_urls():
    ledger = ResearchLedger('botanical survey',budget=Budget(coverage_target=1,min_independent_sources=2))
    first = add(ledger,'https://original.example/report',document())
    mirror = add(ledger,'https://mirror.example/copy','Navigation\n'+document()+'\nCopyright')
    for source in (first,mirror):
        ledger.add_claim('The botanical survey counted 42 samples.',source.source_id)
    report = ledger.report()
    assert report['source_count'] == 2
    claim = report['claims'][0]
    assert claim['source_count'] == 2 and claim['independent_source_count'] == 1
    assert not claim['corroborated'] and not ledger.evidence_sufficient
    assert {e['url'] for e in claim['evidence']} == {first.url,mirror.url}
    assert mirror.document_match['method'] == 'near_duplicate_extracted_text'
    assert 'document_fingerprint' not in report['sources'][0]


def test_checkpoint_keeps_sketch_for_future_mirror_discovery():
    ledger = ResearchLedger('survey')
    original = add(ledger,'https://original.example/report',document())
    ledger.add_claim('The botanical survey counted 42 samples.',original.source_id)
    resumed = ResearchLedger.from_checkpoint(ledger.checkpoint())
    mirror = add(resumed,'https://mirror.example/copy',document())
    resumed.add_claim('The botanical survey counted 42 samples.',mirror.source_id)
    assert resumed.report()['claims'][0]['independent_source_count'] == 1


def test_original_reporting_from_mirror_host_remains_distinct_for_its_own_claim():
    ledger = ResearchLedger('survey')
    original = add(ledger,'https://original.example/report',document())
    mirror = add(ledger,'https://mirror.example/copy',document())
    independent = add(ledger,'https://mirror.example/original',document('astronomical'))
    ledger.add_claim('The botanical survey counted 42 samples.',original.source_id)
    ledger.add_claim('The botanical survey counted 42 samples.',mirror.source_id)
    claim = ledger.add_claim('The astronomical survey measured 19 instruments.',original.source_id)
    ledger.add_claim('The astronomical survey measured 19 instruments.',independent.source_id)
    assert ledger.independent_source_count(claim) == 2
    assert independent.document_group != original.document_group


def test_publisher_and_document_groups_connect_transitively_without_count_inflation():
    ledger = ResearchLedger('survey')
    a = add(ledger,'https://one.example/a',document())
    b = add(ledger,'https://two.example/copy',document())
    c = add(ledger,'https://two.example/other',document('astronomical'))
    d = add(ledger,'https://three.example/independent',document('geological'))
    for source in (a,b,c,d):
        claim = ledger.add_claim('The survey counted 42 samples.',source.source_id)
    assert ledger.independent_source_count(claim) == 2
    assert len(ledger.evidence_publishers) == 2


def test_missing_fingerprints_keep_existing_publisher_behavior():
    ledger = ResearchLedger('survey')
    for host in ('one.example','two.example'):
        source = ledger.add_source('https://'+host)
        claim = ledger.add_claim('The survey counted 42 samples.',source.source_id)
    assert ledger.independent_source_count(claim) == 2
