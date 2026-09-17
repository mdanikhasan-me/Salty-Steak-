from app.backend.research.ledger import Budget,ResearchLedger


def test_unrelated_publishers_cannot_satisfy_topic_coverage():
    ledger=ResearchLedger('How do solar panels convert sunlight into electricity?',budget=Budget(coverage_target=2,validation_rounds=0))
    for url in ['https://one.example','https://two.example']:
        source=ledger.add_source(url)
        ledger.add_claim('Our username search offers premium identity services.',source.source_id)
        ledger.add_claim('The restaurant accepts reservations every weekday evening.',source.source_id)
    assert not ledger.evidence_sufficient
    assert ledger.relevant_claims==[]
    assert ledger.should_stop()!=(True,'evidence_sufficient')


def test_only_relevant_sources_contribute_to_completion():
    ledger=ResearchLedger('Explain solar panels',budget=Budget(coverage_target=1,validation_rounds=0))
    solar=ledger.add_source('https://solar.example');other=ledger.add_source('https://other.example')
    ledger.add_claim('Solar panels generate electricity from sunlight.',solar.source_id)
    ledger.add_claim('Restaurants serve meals every evening.',other.source_id)
    assert not ledger.evidence_sufficient
    ledger.add_claim('Solar panels generate electricity from sunlight.',other.source_id)
    assert ledger.evidence_sufficient


def test_non_latin_topic_is_not_treated_as_empty_query():
    ledger=ResearchLedger('সৌরবিদ্যুৎ',budget=Budget(coverage_target=1,validation_rounds=0))
    a=ledger.add_source('https://one.example');b=ledger.add_source('https://two.example')
    ledger.add_claim('Restaurant reservations are available today.',a.source_id)
    ledger.add_claim('Restaurant reservations are available today.',b.source_id)
    assert ledger.question_subject_words
    assert not ledger.evidence_sufficient


def test_readable_source_does_not_claim_semantic_truth_validation():
    ledger=ResearchLedger('solar panels')
    source=ledger.add_source('https://one.example')
    ledger.add_claim('Solar panels generate electricity.',source.source_id)
    claim=ledger.report()['claims'][0]
    assert claim['validated'] is True
    assert claim['validation_scope']=='source_readability'
    assert claim['support_status']=='single_source'
    assert claim['confidence_scope']=='retrieval_heuristic_not_truth_probability'
