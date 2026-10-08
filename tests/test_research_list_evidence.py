from app.backend.research.loop import statements_from_page


def test_ordered_rule_keeps_context_and_all_levels_despite_keyword_distractors():
    intro='Resolve the base URI according to the following priorities (highest to lowest):'
    items=['Use the BASE element.',
           'Otherwise use metadata provided by the transfer protocol.',
           'By default use the current document address.']
    text='\n'.join(f'The source base URI attribute resolves relative links for definition {i}.' for i in range(60))
    text += '\n\n'+intro+'\n\n'+'\n'.join(items)+'\n\nUnrelated section follows.'
    claims=statements_from_page({'summary':text},question='What ordered sources determine the base URI used to resolve relative links?')
    assert any(intro in c and all(item in c for item in items) for c in claims)


def test_list_grouping_never_reorders_or_invents_numbering():
    text='The permitted fallback order is:\n\nCached signed record\nVerified registry record\nManual review\n\nOther details.'
    claims=statements_from_page({'summary':text},question='What is the permitted fallback order?')
    group=next(c for c in claims if all(x in c for x in ('Cached','registry','Manual')))
    assert group.index('Cached') < group.index('registry') < group.index('Manual')
    assert '1.' not in group


def test_unbounded_lists_are_not_copied_into_a_single_claim():
    text='All measurements are:\n\n'+'\n'.join('A measurement has an observed value. '*20 for _ in range(40))
    assert all(len(c)<=2000 for c in statements_from_page({'summary':text},question='All measurements'))


def test_complete_list_survives_ledger_review_and_answer_selection():
    from app.backend.research.ledger import ResearchLedger
    from app.backend.chat.runners import LiveRunners, _select_finaliser_findings
    from app.backend.research.passages import contextual_list_blocks
    question='Read https://manual.example/spec and explain the ordered resolver fallback sources.'
    text=('The resolver fallback sources have the following order:\n\n'
          'The explicit address in the record.\n'
          'The transport metadata provided by the protocol.\n'
          'The current document location.\n\n')
    text+='\n'.join(f'The resolver specification source describes fallback attributes in example {i}.' for i in range(60))
    ledger=ResearchLedger(question)
    source=ledger.add_source('https://manual.example/spec','Resolver specification')
    ledger.ingest(source,statements_from_page({'summary':text},question=question),
                  context_blocks=contextual_list_blocks(text))
    ledger=ResearchLedger.from_checkpoint(ledger.checkpoint())
    assert any(c.structure=='contextual_list' for c in ledger.claims.values())
    runner=LiveRunners(generate_research_review=lambda *args,**kwargs:'{}')
    runner._assess_coverage(ledger)
    selected=_select_finaliser_findings(question,ledger.report()['claims'])
    for packet in (runner._coverage_findings, selected):
        assert any(all(part in claim['text'] for part in ('explicit address','transport metadata','document location'))
                   and claim['evidence'][0]['url']==source.url for claim in packet)


def test_list_binding_does_not_cross_a_section_boundary():
    from app.backend.research.passages import contextual_list_blocks
    assert contextual_list_blocks('Reference:\n\nSingle definition.\n\nNext section\nUnrelated rule.') == []


def test_loop_retains_structure_in_source_bound_evidence(tmp_path):
    from app.backend.research.loop import ResearchLoop
    from app.backend.research.ledger import Budget
    text=('The resolution order is:\n\n'
          'Use the explicit address provided by the author.\n'
          'Otherwise use the address supplied by the transport.\n'
          'Otherwise use the document location.')
    report=ResearchLoop('What is the resolution order?',
        search=lambda _: [{'url':'https://manual.example/resolution'}],
        read=lambda url:{'url':url,'summary':text},
        checkpoint_path=tmp_path/'checkpoint.json',
        budget=Budget(max_queries=1,max_sources=1,validation_rounds=0)).run()
    group=next(c for c in report['claims'] if c.get('structure')=='contextual_list')
    assert 'transport' in group['text'] and 'document location' in group['text']
    assert group['evidence'][0]['url']=='https://manual.example/resolution'


def test_structure_does_not_override_irrelevance_or_disputes():
    from app.backend.chat.runners import _select_finaliser_findings
    source={'url':'https://manual.example','title':'Manual'}
    normal={'claim':'clm-1','text':'The orchid survey counted 42 specimens.', 'evidence':[source]}
    irrelevant={'claim':'clm-2','text':'Resolver fallback: transport address then document address.',
                'structure':'contextual_list','evidence':[source]}
    dispute={'claim':'clm-3','text':'Orchid survey: 43 specimens.',
             'structure':'contextual_list','disputed':True,'contradicts':[],'evidence':[source]}
    assert _select_finaliser_findings('How many orchid specimens were counted?',
        [irrelevant,dispute,normal],limit=1)==[normal]


def test_partial_repetition_cannot_corroborate_whole_compound_claim():
    from app.backend.research.ledger import ResearchLedger
    for reverse in (False,True):
        ledger=ResearchLedger('What fallback order applies?')
        a=ledger.add_source('https://one.example/spec')
        b=ledger.add_source('https://two.example/article')
        short='The explicit address supplied by the document author has precedence.'
        whole='Resolve addresses in this order: '+short+' Otherwise use the transport metadata.'
        if reverse:
            ledger.add_claim(short,b.source_id)
        group=ledger.add_claim(whole,a.source_id,structure='contextual_list')
        if not reverse:
            ledger.add_claim(short,b.source_id)
        assert group.text==whole and group.sources==[a.source_id]
        assert len(ledger.claims)==2
        ledger.add_claim(whole,b.source_id,structure='contextual_list')
        assert group.sources==[a.source_id,b.source_id]
