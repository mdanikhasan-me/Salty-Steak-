"""One domain's rules must not answer another domain's question.

Asked "hi" with Research switched on, the installed application searched the
web and replied with a paragraph that included:

    No prices, products, or specific item availability...

Nothing in the conversation was about shopping. The finaliser sends a single
unconditional system instruction — about fifteen sentences on prices, sellers,
stock, catalogue ranges and currency marks — for every research question
whatever its subject, and the payload beside it always carries a
``verified_products`` key even when it is empty. Told at length about products,
the model dutifully reported that there were none.

The rule here is evidence-driven, never keyword-driven: the commerce guidance
appears because priced observations exist, not because a question contained a
word.
"""

from __future__ import annotations

from app.backend.chat.runners import (
    LiveRunners,
    _attach_validated_citations,
    _ensure_disagreement_answer,
    _research_activity_journal,
    _status_answer_uses_latest,
    _status_fallback_answer,
    _trim_unrequested_status_scope,
    _select_finaliser_findings,
    finaliser_instruction,
    finaliser_payload,
)


def test_percentage_is_not_a_software_release():
    finding = {'text': 'Our search service delivers 99.9% availability.', 'validated': True}
    assert _status_fallback_answer('What is the latest Python release?', [finding]) == ''


def test_requested_documentation_survives_a_verbose_secondary_source():
    records = [
        {'claim':f'c{i}', 'text':f'Python list mutability supports operation number {i}.',
         'evidence':[{'url':'https://secondary.example/python', 'domain':'secondary.example',
                      'title':'Python lists'}]}
        for i in range(12)
    ]
    records.append({'claim':'official','text':'Lists are mutable sequences.',
                    'evidence':[{'url':'https://docs.python.org/3/library/stdtypes.html',
                                 'domain':'docs.python.org','title':'Python documentation'}]})
    chosen = _select_finaliser_findings('What does Python documentation say about list mutability?',records)
    assert any(record['claim']=='official' for record in chosen)


def test_publisher_diversity_cannot_displace_requested_measurements():
    primary={'url':'https://paper.example/study','domain':'paper.example','title':'Instrument calibration'}
    records=[
        {'claim':'time','text':'The instrument calibration time constant is 36 seconds.','evidence':[primary]},
        {'claim':'sensitivity','text':'The instrument sensitivity was 10.7 volts per millimeter in January 1911 and 13.0 in May 1914, measured by fortnightly scale tests.','evidence':[primary]},
    ]
    records += [{'claim':f'other{i}','text':'The instrument is in an observatory with historical records.',
        'evidence':[{'url':f'https://publisher{i}.example','domain':f'publisher{i}.example'}]} for i in range(12)]
    selected=_select_finaliser_findings('What was the instrument sensitivity in January 1911 and May 1914, its time constant and calibration method?',records)
    assert {'time','sensitivity'} <= {r['claim'] for r in selected}


def test_citations_do_not_add_a_read_page_absent_from_answer_evidence():
    evidence={'url':'https://docs.example/lists','title':'List documentation','validation':'validated'}
    unrelated={'url':'https://news.example/unrelated','title':'Python list mutability news','validation':'validated'}
    answer=_attach_validated_citations('Lists can change.',
        [{'text':'Lists can change.','evidence':[evidence]}],
        sources=[unrelated,evidence],question='Python list mutability')
    assert 'docs.example/lists' in answer
    assert 'news.example/unrelated' not in answer


def test_non_release_numbers_and_unvalidated_claims_are_not_release_evidence():
    assert _status_fallback_answer('Latest Python version?', [{'text': 'Score 3.14 out of 5.'}]) == ''
    assert _status_fallback_answer('Latest Python version?', [{'text': 'Python 9.9 is released.', 'validated': False}]) == ''
    assert _status_fallback_answer('Latest Python version?', [{'text': 'ExampleDB 9.9 is released.', 'validated': True}]) == ''


def test_release_override_binds_each_version_to_its_own_subject():
    findings = [{'text': 'Python 3.14 was released; ExampleDB 9.9 is also available.', 'validated': True}]
    answer = _status_fallback_answer('What is the latest Python release?', findings)
    assert answer.startswith('3.14 ')
    assert '9.9' not in answer


def test_release_context_cannot_turn_percentages_or_another_product_into_a_version():
    for text in [
        'Python release notes report 99.9 percent test coverage.',
        'Python release notes report 99.9 per cent test coverage.',
        'ExampleDB 9.9 released with a Python client.',
        'Python 3.14 is compatible with ExampleDB 9.9 released yesterday.',
    ]:
        assert _status_fallback_answer('What is the latest Python release?', [{'text': text, 'validated': True}]) == ''


def test_release_subject_binding_is_not_specific_to_one_product():
    findings = [{'text': 'Nebula 2.4.1 is the latest stable release; ExampleDB 9.9 is released.', 'validated': True}]
    assert _status_fallback_answer('Latest Nebula version?', findings).startswith('2.4.1 ')


def test_failed_synthesis_does_not_return_unrelated_claims_as_the_answer():
    report={'claims':[{'text':'Our username search delivers 99.9% availability.', 'validated':True}], 'sources':[]}
    for reply in ['', 'I will research the question and verify the sources.', '{"query": null}']:
        result=LiveRunners(generate=lambda _, answer=reply:answer)._answer_from('What is the latest Python release?', report)
        assert 'could not produce a supported answer' in result
        assert '99.9' not in result
        assert 'Validated sources' not in result

PRICED = [
    {
        "product": "Lexar NM790 2TB",
        "price_display": "40,999 Tk",
        "currency_code": "BDT",
        "seller": "ryans.com",
        "stock": "in_stock",
        "url": "https://www.ryans.com/lexar-nm790-2tb",
    }
]


def test_a_question_with_no_priced_evidence_gets_no_commerce_rules() -> None:
    generic = finaliser_instruction(observations=[]).casefold()

    for word in ("price", "in stock", "seller", "catalogue", "currency"):
        assert word not in generic


def test_the_base_rules_survive_whatever_the_subject() -> None:
    """Restraint is not removal. The rules that keep an answer honest stay."""

    generic = finaliser_instruction(observations=[]).casefold()

    assert "only the findings" in generic
    assert "do not describe the search" in generic
    assert "never combine" in generic
    assert "do not estimate" in generic


def test_priced_evidence_brings_the_commerce_rules_back() -> None:
    commerce = finaliser_instruction(observations=PRICED).casefold()

    assert "verified_products" in commerce
    assert "in stock" in commerce
    assert "character for character" in commerce
    # The base rules are still there underneath.
    assert "do not estimate" in commerce


def test_an_empty_product_list_is_left_out_rather_than_sent_empty() -> None:
    """An empty shopping-shaped field still says the answer is about shopping."""

    payload = finaliser_payload(
        question="hi", observations=[], evidence_limits={}, findings=[]
    )

    assert "verified_products" not in payload
    assert "evidence_limits" not in payload
    assert payload["question"] == "hi"


def test_real_products_are_still_handed_over_whole() -> None:
    payload = finaliser_payload(
        question="cheapest 2TB Gen4 NVMe in Bangladesh",
        observations=PRICED,
        evidence_limits={"stock_unconfirmed": 0},
        findings=[{"text": "a finding", "sources": 2, "disputed": False}],
    )

    assert payload["verified_products"][0]["price"] == "40,999 Tk"
    assert payload["verified_products"][0]["currency"] == "BDT"
    assert payload["verified_products"][0]["stock"] == "in_stock"
    assert payload["evidence_limits"] == {"stock_unconfirmed": 0}


def test_an_observation_with_no_price_does_not_count_as_commerce() -> None:
    """A page can be observed without any offer being bound on it."""

    unpriced = [{"product": "Lexar NM790 2TB", "url": "https://example.com"}]

    assert "price" not in finaliser_instruction(observations=unpriced).casefold()


def test_answer_links_are_limited_to_validated_evidence_urls() -> None:
    findings = [
        {
            "text": "A supported finding.",
            "evidence": [
                {
                    "title": "Official report",
                    "url": "https://official.example/report",
                    "validation": "validated",
                }
            ],
        }
    ]

    answer = _attach_validated_citations(
        "Use [this invented link](https://fake.example/item) for the result.",
        findings,
    )

    assert "https://fake.example" not in answer
    assert "https://official.example/report" in answer
    assert "Validated sources:" in answer


def test_removed_unvalidated_link_does_not_leave_a_dangling_list_marker() -> None:
    findings = [
        {
            "text": "A supported finding.",
            "evidence": [
                {
                    "title": "Official report",
                    "url": "https://official.example/report",
                    "validation": "validated",
                }
            ],
        }
    ]

    answer = _attach_validated_citations(
        "Supported.\n\n- [Official report](https://official.example/report)\n"
        "- https://invented.example/report",
        findings,
    )

    assert "\n-\n" not in answer
    assert not any(line.strip() == "-" for line in answer.splitlines())
    assert "https://invented.example" not in answer


def test_finaliser_receives_claim_level_source_records() -> None:
    finding = {
        "text": "A supported finding.",
        "source_count": 1,
        "sources": [
            {
                "title": "Official report",
                "url": "https://official.example/report",
                "validation": "validated",
            }
        ],
        "disputed": False,
    }

    payload = finaliser_payload(
        question="What happened?",
        observations=[],
        evidence_limits={},
        findings=[finding],
    )

    assert payload["findings"][0]["sources"][0]["url"] == (
        "https://official.example/report"
    )


def test_narrow_question_prioritises_relevant_claims_and_keeps_a_dispute() -> None:
    claims = [
        {
            "text": "Python 3.14 adds template string literals.",
            "source_count": 8,
            "independent_source_count": 1,
            "disputed": False,
        },
        {
            "text": "Python 3.14.7 is the current maintenance release.",
            "source_count": 2,
            "independent_source_count": 2,
            "disputed": False,
        },
        {
            "claim": "clm-old",
            "text": "One source labels Python 3.14.6 as the current release status.",
            "source_count": 1,
            "independent_source_count": 1,
            "disputed": True,
            "contradicts": ["clm-new"],
        },
        {
            "claim": "clm-new",
            "text": "Another source labels Python 3.14.7 as the current release status.",
            "source_count": 1,
            "independent_source_count": 1,
            "disputed": True,
            "contradicts": ["clm-old"],
        },
    ]

    selected = _select_finaliser_findings(
        "What is the current Python 3.14 release status?", claims
    )

    assert selected[0]["text"].startswith("Python 3.14.7")
    assert any(item["disputed"] for item in selected)


def test_current_status_packet_excludes_adjacent_artifacts_and_issue_status() -> None:
    claims = [
        {
            "text": "Python 3.14.7 is the current maintenance release.",
            "source_count": 2,
            "disputed": False,
        },
        {
            "text": "Python 3.14 release artifacts no longer include PGP signatures.",
            "source_count": 8,
            "disputed": False,
        },
        {
            "text": "In python/cpython issue #155974 Status: Open.",
            "source_count": 1,
            "disputed": False,
        },
    ]

    selected = _select_finaliser_findings(
        "What is the current Python 3.14 release status?", claims
    )

    assert [item["text"] for item in selected] == [claims[0]["text"]]


def test_current_status_packet_places_the_newest_matching_patch_first() -> None:
    claims = [
        {
            "text": "Python 3.14.1 is the first maintenance release.",
            "source_count": 1,
            "disputed": False,
        },
        {
            "text": "Python 3.14.7 is the current maintenance release.",
            "source_count": 1,
            "disputed": False,
        },
        {
            "text": "Python 3.13.15 is the current maintenance release.",
            "source_count": 3,
            "disputed": False,
        },
    ]

    selected = _select_finaliser_findings(
        "What is the current Python 3.14 release status?", claims
    )

    assert selected[0]["text"].startswith("Python 3.14.7")


def test_status_synthesis_cannot_call_an_older_patch_current() -> None:
    question = "What is the current Python 3.14 release status?"
    findings = [
        {
            "text": "Python 3.14.7 is the seventh maintenance release.",
            "disputed": False,
        },
        {
            "text": "Python 3.14.1 is the first maintenance release.",
            "disputed": False,
        },
    ]

    assert _status_answer_uses_latest(question, "Python 3.14.1 is current.", findings) is False
    assert _status_answer_uses_latest(question, "Python 3.14.7 is current.", findings) is True
    assert _status_fallback_answer(question, findings).startswith(
        "3.14.7 is the latest validated maintenance release"
    )


def test_patch_status_does_not_collapse_maintenance_and_security_lifetimes() -> None:
    answer = _trim_unrequested_status_scope(
        "What is the current Python 3.14 release status?",
        "Python 3.14.7 is current, with active support through October 31, 2030. "
        "It is the seventh maintenance release.",
    )

    assert answer == (
        "Python 3.14.7 is current. It is the seventh maintenance release."
    )
    assert _trim_unrequested_status_scope(
        "What is the current Python 3.14 release status?",
        "Python 3.14.7 is current, with active support extending through "
        "October 31, 2030.",
    ) == "Python 3.14.7 is current."
    assert _trim_unrequested_status_scope(
        "What is the Python 3.14 support lifecycle?",
        "Python 3.14.7 is current, with active support through October 31, 2030.",
    ).endswith("October 31, 2030.")


def test_status_activity_names_exact_patch_confirmation() -> None:
    journal = _research_activity_journal(
        {
            "waves": [],
            "status_target_version": "3.14.7",
            "status_target_publisher_count": 2,
            "relevant_disputed": 0,
            "rejected_sources": [{"url": "https://challenge.example"}],
        }
    )

    assert journal[-1]["detail"] == (
        "2 publishers confirmed 3.14.7 · 0 material disagreements · 1 rejected link"
    )


def test_citation_fallback_diversifies_publishers_not_subdomains() -> None:
    findings = [
        {
            "text": "A supported release fact.",
            "evidence": [
                {
                    "title": "Official release",
                    "url": "https://www.python.org/downloads/release/python-3147",
                    "validation": "validated",
                },
                {
                    "title": "Official blog",
                    "url": "https://blog.python.org/2026/08/python-3147",
                    "validation": "validated",
                },
                {
                    "title": "Independent review",
                    "url": "https://technical.example/python-3147",
                    "validation": "validated",
                },
            ],
        }
    ]

    answer = _attach_validated_citations("Python 3.14.7 is released.", findings)

    assert "https://www.python.org/downloads/release/python-3147" in answer
    assert "https://technical.example/python-3147" in answer
    assert "https://blog.python.org/2026/08/python-3147" in answer
    assert answer.index("technical.example") < answer.index("blog.python.org")


def test_numbered_footnotes_are_deduplicated_and_unvalidated_urls_removed() -> None:
    findings = [
        {
            "text": "A supported release fact.",
            "evidence": [
                {
                    "title": "Official release",
                    "url": "https://official.example/release",
                    "validation": "validated",
                }
            ],
        }
    ]
    answer = _attach_validated_citations(
        "Released.[^1] Reconfirmed.[^2] Invented.[^3]\n\n"
        "[^1]: https://official.example/release\n"
        "[^2]: https://official.example/release/\n"
        "[^3]: https://invented.example/release",
        findings,
    )

    assert answer.count("https://official.example/release") == 1
    assert "https://invented.example" not in answer
    assert "[^2]" not in answer
    assert "[^3]" not in answer


def test_bare_urls_become_three_focused_diverse_markdown_sources() -> None:
    sources = [
        {
            "title": "Python Release Python 3.14.7 | Python.org",
            "url": "https://www.python.org/downloads/release/python-3147",
            "validation": "validated",
            "content_characters": 6000,
        },
        {
            "title": "Python 3.14 support lifecycle",
            "url": "https://versionlog.example/python/3.14",
            "validation": "validated",
            "content_characters": 5000,
        },
        {
            "title": "Python 3.14 released",
            "url": "https://technical.example/python-3-14-release",
            "validation": "validated",
            "content_characters": 4000,
        },
        {
            "title": "Older Python release page",
            "url": "https://www.python.org/downloads/release/python-3140",
            "validation": "validated",
            "content_characters": 3000,
        },
    ]

    answer = _attach_validated_citations(
        "Python 3.14.7 is current.\n\n"
        "https://www.python.org/downloads/release/python-3147\n"
        "https://invented.example/not-read",
        [],
        sources=sources,
        question="What is the current Python 3.14 release status?",
    )

    assert "\nhttps://" not in answer
    assert "invented.example" not in answer
    assert answer.count("](") == 3
    assert "versionlog.example" in answer
    assert "technical.example" in answer


def test_internal_source_markers_become_descriptive_links_or_disappear() -> None:
    findings = [
        {
            "text": "Python 3.14.7 is current.",
            "evidence": [
                {
                    "title": "Python 3.14.7 release",
                    "url": "https://official.example/python-3147",
                    "validation": "validated",
                }
            ],
        }
    ]

    answer = _attach_validated_citations(
        "Based on the provided findings, Python is current "
        "[src-1](https://official.example/python-3147), with an older note src-4.",
        findings,
        question="What is the current Python version?",
    )

    assert not answer.startswith("Based on")
    assert "src-" not in answer
    assert "[Python 3.14.7 release]" in answer
    assert "note." in answer


def test_a_url_used_as_its_own_link_label_becomes_the_source_title() -> None:
    findings = [
        {
            "text": "Python 3.14.7 is current.",
            "evidence": [
                {
                    "title": "Python 3.14.7 release",
                    "url": "https://official.example/python-3147",
                    "validation": "validated",
                }
            ],
        }
    ]

    answer = _attach_validated_citations(
        "Python is current "
        "[https://official.example/python-3147]"
        "(https://official.example/python-3147).",
        findings,
        question="What is the current Python version?",
    )

    assert "[Python 3.14.7 release]" in answer
    assert "[https://" not in answer


def test_requested_disagreement_check_is_explicit_when_none_is_material() -> None:
    answer = _ensure_disagreement_answer(
        "Identify any disagreement between sources.",
        "Python 3.14.7 is the current maintenance release.",
        [{"text": "Python 3.14.7 is current.", "disputed": False}],
    )

    assert "No material disagreement" in answer
