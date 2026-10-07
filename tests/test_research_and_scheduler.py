from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.backend.automation.policy import PolicyEngine
from app.backend.chat.task_runtime import TaskContext
from app.backend.connectors import ConnectorManager, LocalMailConnector
from app.backend.research import Budget, ResearchLedger, ResearchLoop, statements_from_page
from app.backend.scheduler import (
    TRIGGER_EVENT,
    TRIGGER_NOW,
    TRIGGER_RECURRING,
    ScheduleError,
    Scheduler,
)

# ------------------------------------------------------------------- ledger


def test_page_extraction_finds_relevant_text_after_navigation_and_early_sections():
    page={'summary': ' '.join(f'The unrelated section contains example number {i}.' for i in range(60))
          + ' Python lists are mutable and their elements can be changed in place.'}
    found=statements_from_page(page, question='What is Python list mutability?',limit=4)
    assert len(found)==4
    assert 'mutable' in found[0]


def test_the_same_claim_from_two_sources_is_one_claim_with_two_sources() -> None:
    ledger = ResearchLedger("how tall is the tower")
    first = ledger.add_source("https://a.example/tower", "A")
    second = ledger.add_source("https://b.example/tower", "B")

    ledger.add_claim("The tower is 324 metres tall including its antenna.", first.source_id)
    claim = ledger.add_claim(
        "The tower is 324 metres tall including its antenna.", second.source_id
    )

    assert len(ledger.claims) == 1
    assert claim.corroborated is True
    assert len(claim.sources) == 2
    # Two independent sources is worth more than one.
    assert claim.confidence > 0.5


def test_a_contradiction_between_sources_is_kept_not_averaged_away() -> None:
    ledger = ResearchLedger("how tall is the tower")
    first = ledger.add_source("https://a.example", "A")
    second = ledger.add_source("https://b.example", "B")

    original = ledger.add_claim("The tower is 324 metres tall.", first.source_id)
    conflicting = ledger.add_claim("The tower is 330 metres tall.", second.source_id)

    assert original.claim_id != conflicting.claim_id
    assert conflicting.disputed is True
    assert original.claim_id in conflicting.contradicts
    # Neither is trusted while they disagree.
    assert original.confidence <= 0.4
    assert len(ledger.disputed_claims) == 2


def test_a_negated_statement_is_a_disagreement_not_a_duplicate() -> None:
    ledger = ResearchLedger("is the bridge open")
    first = ledger.add_source("https://a.example", "A")
    second = ledger.add_source("https://b.example", "B")

    ledger.add_claim("The bridge is open to traffic this weekend.", first.source_id)
    later = ledger.add_claim(
        "The bridge is not open to traffic this weekend.", second.source_id
    )

    assert later.disputed is True
    assert len(ledger.claims) == 2


@pytest.mark.parametrize('left,right', [
    ('Company Alpha acquired Company Beta in 2025.', 'Company Beta acquired Company Alpha in 2025.'),
    ('The policy permits employees to publish the customer records.', 'The policy prohibits employees from publishing the customer records.'),
    ('Product Alpha costs 100 and Product Beta costs 200.', 'Product Alpha costs 200 and Product Beta costs 100.'),
    ('The service supports encryption.', 'The service supports encryption for enterprise customers only.'),
])
def test_word_overlap_does_not_manufacture_corroboration(left, right):
    ledger = ResearchLedger('Compare these claims')
    a = ledger.add_source('https://a.example/report')
    b = ledger.add_source('https://b.example/report')
    first = ledger.add_claim(left, a.source_id)
    second = ledger.add_claim(right, b.source_id)
    assert first.claim_id != second.claim_id
    assert ledger.independent_source_count(first) == 1
    assert ledger.independent_source_count(second) == 1


def test_opposite_permission_verbs_are_preserved_as_a_dispute():
    ledger = ResearchLedger('Does the policy permit publication?')
    a = ledger.add_source('https://a.example/report')
    b = ledger.add_source('https://b.example/report')
    first = ledger.add_claim('The policy permits publication of customer records.', a.source_id)
    second = ledger.add_claim('The policy prohibits publication of customer records.', b.source_id)
    assert first.claim_id in second.contradicts


def test_identical_compound_permission_statement_is_not_a_dispute():
    ledger=ResearchLedger('Who may drive?')
    a=ledger.add_source('https://a.example/report')
    b=ledger.add_source('https://b.example/report')
    statement='The policy allows adults to drive and prohibits minors from driving.'
    first=ledger.add_claim(statement,a.source_id)
    second=ledger.add_claim(statement,b.source_id)
    assert first.claim_id==second.claim_id
    assert not first.disputed
    assert ledger.independent_source_count(first)==2


def test_permissions_for_different_subjects_are_not_a_dispute():
    ledger=ResearchLedger('Who may drive?')
    a=ledger.add_source('https://a.example/report')
    b=ledger.add_source('https://b.example/report')
    first=ledger.add_claim('The policy allows adults to drive on public roads.',a.source_id)
    second=ledger.add_claim('The policy prohibits minors from driving on public roads.',b.source_id)
    assert first.claim_id!=second.claim_id
    assert not first.disputed and not second.disputed


def test_opposite_rules_for_different_directions_are_not_a_dispute():
    ledger=ResearchLedger('Firewall rules')
    a=ledger.add_source('https://a.example/report')
    b=ledger.add_source('https://b.example/report')
    first=ledger.add_claim('The firewall allows traffic from the server.',a.source_id)
    second=ledger.add_claim('The firewall prohibits traffic to the server.',b.source_id)
    assert first.claim_id!=second.claim_id
    assert not first.disputed and not second.disputed


def test_disagreements_are_put_in_front_of_the_model_first() -> None:
    ledger = ResearchLedger("q")
    a = ledger.add_source("https://a.example", "A")
    b = ledger.add_source("https://b.example", "B")
    ledger.add_claim("Registration closes on the fifth of September.", a.source_id)
    ledger.add_claim("Registration closes on the ninth of September.", b.source_id)
    ledger.add_claim("The campus library opens at eight in the morning.", a.source_id)

    evidence = ledger.evidence_for_model()

    # A contradiction is the finding most likely to change an answer.
    assert evidence[0]["disputed"] is True


def test_the_same_source_is_never_read_twice() -> None:
    ledger = ResearchLedger("q")
    assert ledger.add_source("https://a.example/page/") is not None
    assert ledger.add_source("https://a.example/page") is None
    assert ledger.add_source("https://a.example/page#section") is None


def test_provenance_survives_to_the_answer() -> None:
    ledger = ResearchLedger("q")
    source = ledger.add_source("https://a.example/report", "The Report")
    claim = ledger.add_claim("Enrolment rose by twelve percent.", source.source_id)

    provenance = ledger.provenance(claim.claim_id)

    assert provenance[0]["url"] == "https://a.example/report"
    assert provenance[0]["title"] == "The Report"


def test_report_binds_each_claim_to_validated_source_records() -> None:
    ledger = ResearchLedger("q")
    source = ledger.add_source(
        "https://a.example/report",
        "The Report",
        content_sha256="a" * 64,
        content_characters=1200,
    )
    ledger.add_claim("Enrolment rose by twelve percent.", source.source_id)

    claim = ledger.report()["claims"][0]

    assert claim["evidence"][0]["url"] == "https://a.example/report"
    assert claim["evidence"][0]["validation"] == "validated"
    assert claim["validated"] is True


def test_two_pages_on_one_domain_are_not_independent_corroboration() -> None:
    ledger = ResearchLedger("q")
    first = ledger.add_source("https://same.example/a")
    second = ledger.add_source("https://same.example/b")
    ledger.add_claim("The tower is 324 metres tall.", first.source_id)
    ledger.add_claim("The tower is 324 metres tall.", second.source_id)

    assert len(ledger.corroborated_claims) == 0
    assert ledger.report()["claims"][0]["independent_source_count"] == 1


def test_subdomains_of_one_publisher_are_not_independent_corroboration() -> None:
    ledger = ResearchLedger("q")
    docs = ledger.add_source("https://docs.python.org/3/whatsnew/3.14.html")
    blog = ledger.add_source("https://blog.python.org/2025/10/python-3140-final.html")
    ledger.add_claim("Python 3.14 was released on 7 October 2025.", docs.source_id)
    ledger.add_claim("Python 3.14 was released on 7 October 2025.", blog.source_id)

    claim = ledger.report()["claims"][0]

    assert claim["source_count"] == 2
    assert claim["independent_source_count"] == 1
    assert claim["corroborated"] is False


def test_repository_and_document_mirrors_do_not_fake_independence() -> None:
    ledger = ResearchLedger("Python release status")
    official = ledger.add_source(
        "https://peps.python.org/pep-0745",
        "PEP 745 | peps.python.org",
    )
    repository = ledger.add_source(
        "https://github.com/python/peps/blob/main/peps/pep-0745.rst",
        "python/peps on GitHub",
    )
    document_host = ledger.add_source(
        "https://www.scribd.com/document/875941669/pep-0745-rst",
        "Python 3.14 Release Schedule Details",
    )
    for source in (official, repository, document_host):
        ledger.add_claim(
            "Python 3.14 receives bugfix releases for about two years.",
            source.source_id,
        )

    claim = ledger.report()["claims"][0]

    assert claim["source_count"] == 3
    assert claim["independent_source_count"] == 1
    assert claim["corroborated"] is False


def test_attributed_translation_mirror_uses_the_origin_publisher() -> None:
    ledger = ResearchLedger("Python release status")
    official = ledger.add_source(
        "https://peps.python.org/pep-0745",
        "PEP 745 | peps.python.org",
    )
    mirror = ledger.add_source(
        "https://peps.pythonlang.de/pep-0745",
        "PEP 745 - Python 3.14 Release Schedule | peps.python.org",
    )
    ledger.add_claim("Python 3.14 has a published release schedule.", official.source_id)
    ledger.add_claim("Python 3.14 has a published release schedule.", mirror.source_id)

    assert ledger.report()["claims"][0]["independent_source_count"] == 1


def test_an_unresolved_pronoun_does_not_corroborate_a_named_release() -> None:
    ledger = ResearchLedger("q")
    official = ledger.add_source("https://www.python.org/downloads/release/python-3147")
    independent = ledger.add_source("https://technical.example/python-3147")
    ledger.add_claim(
        "Python 3.14.7 contains around 499 bug fixes, build improvements, and "
        "documentation changes from 86 contributors since Python 3.14.6.",
        official.source_id,
    )
    ledger.add_claim(
        "It contains around 499 bug fixes, build improvements, and documentation "
        "changes from 86 contributors since Python 3.14.6.",
        independent.source_id,
    )

    claims = ledger.report()["claims"]

    # The second page's "It" has no resolved subject. It may refer to the
    # older release named at the end, so overlap alone cannot corroborate it.
    assert len(claims) == 2
    assert all(claim["independent_source_count"] == 1 for claim in claims)
    assert all(claim["corroborated"] is False for claim in claims)


def test_different_version_entities_do_not_merge_or_create_false_disputes() -> None:
    ledger = ResearchLedger("q")
    first = ledger.add_source("https://python.example/314")
    second = ledger.add_source("https://technical.example/39")
    ledger.add_claim("Python 3.14 was released in October 2025.", first.source_id)
    ledger.add_claim("Python 3.9 followed in October 2020.", second.source_id)

    claims = ledger.report()["claims"]

    assert len(claims) == 2
    assert all(claim["disputed"] is False for claim in claims)


def test_multi_release_page_numbers_do_not_create_a_false_dispute() -> None:
    ledger = ResearchLedger("q")
    first = ledger.add_source("https://python.example/3147")
    second = ledger.add_source("https://technical.example/3147")
    ledger.add_claim(
        "Python 3.14.7 is available; Python 3.13.15 contains 400 fixes.",
        first.source_id,
    )
    ledger.add_claim(
        "Python 3.14.7 contains 499 fixes since Python 3.14.6.",
        second.source_id,
    )

    claims = ledger.report()["claims"]

    assert len(claims) == 2
    assert all(claim["disputed"] is False for claim in claims)


# ------------------------------------------------------------------ stopping


@pytest.mark.parametrize(
    "budget,reason",
    [
        (Budget(max_sources=2), "source_budget"),
        (Budget(max_queries=1, max_sources=99), "query_budget"),
    ],
)
def test_research_stops_for_a_named_reason(budget, reason) -> None:
    ledger = ResearchLedger("q", budget=budget)
    ledger.record_query("first")
    for index in range(3):
        ledger.add_source(f"https://s{index}.example")

    stop, actual = ledger.should_stop()
    assert stop is True
    assert actual == reason


def test_research_stops_when_nothing_new_is_being_learned() -> None:
    ledger = ResearchLedger("q", budget=Budget(barren_limit=2, coverage_target=99))
    first = ledger.add_source("https://a.example")
    ledger.ingest(first, ["Enrolment rose by twelve percent this year."])

    for index in range(2):
        source = ledger.add_source(f"https://dup{index}.example")
        ledger.ingest(source, ["Enrolment rose by twelve percent this year."])

    stop, reason = ledger.should_stop()
    assert stop is True
    assert reason == "no_new_information"


def test_research_profiles_have_truthful_time_ceiling_and_extended_validation() -> None:
    verification = Budget.for_profile("verification")
    instant = Budget.for_profile("instant")
    cooking = Budget.for_profile("cooking")

    assert verification.max_seconds == 180
    assert instant.max_seconds == 300
    assert cooking.max_seconds == 14_400
    assert cooking.validation_rounds == 2
    assert cooking.max_sources > instant.max_sources > verification.max_sources
    assert cooking.max_sources_per_query == 8
    assert instant.max_sources_per_query == 5
    assert verification.max_sources_per_query == 4


def test_current_status_requires_independent_publishers_on_the_status_claim() -> None:
    ledger = ResearchLedger(
        "Research the current Python 3.14 release status using official Python "
        "sources and an independent technical source.",
        budget=Budget(coverage_target=1, validation_rounds=1),
    )
    official = ledger.add_source(
        "https://www.python.org/downloads/release/python-3147"
    )
    adjacent = ledger.add_source("https://technical.example/python-features")
    ledger.add_claim(
        "Python 3.14.7 is the current maintenance release.", official.source_id
    )
    ledger.add_claim(
        "Python template strings support structured processing.", adjacent.source_id
    )
    ledger.add_claim(
        "Python 3.14 was released in October 2025.", adjacent.source_id
    )
    ledger.validation_rounds_completed = 1

    assert ledger.evidence_sufficient is False
    assert ledger.relevant_evidence_publishers == {"python.org", "technical.example"}
    assert ledger.status_target_publishers == {"python.org"}
    assert ledger.should_stop() == (False, "")

    ledger.add_claim(
        "Python 3.14.7 is the current maintenance release.", adjacent.source_id
    )

    assert ledger.evidence_sufficient is True
    assert ledger.relevant_evidence_publishers == {"python.org", "technical.example"}
    assert ledger.should_stop() == (True, "evidence_sufficient")


def test_same_publisher_release_pages_do_not_manufacture_a_disagreement() -> None:
    ledger = ResearchLedger("current Python 3.14 release status")
    downloads = ledger.add_source("https://www.python.org/downloads/python-3146")
    blog = ledger.add_source("https://blog.python.org/python-3147")
    independent = ledger.add_source("https://technical.example/python-3147")
    ledger.add_claim(
        "Python 3.14.6 is the current maintenance release.", downloads.source_id
    )
    ledger.add_claim(
        "Python 3.14.7 is the current maintenance release.", blog.source_id
    )

    assert ledger.disputed_claims == []

    ledger.add_claim(
        "Python 3.14.8 is the current maintenance release.", independent.source_id
    )

    assert len(ledger.disputed_claims) == 3


def test_page_furniture_is_not_treated_as_evidence() -> None:
    statements = statements_from_page(
        {
            "summary": (
                "Skip to content. We use cookies to improve your experience. "
                "The department enrolled twelve thousand students in 2026. "
                "All rights reserved."
            )
        }
    )
    assert statements == ["The department enrolled twelve thousand students in 2026."]


# ---------------------------------------------------------------------- loop


def test_requested_documentation_is_read_before_the_wave_budget_is_spent():
    opened=[]
    candidates=[{'url':f'https://blog{i}.example','title':'List tutorial'} for i in range(6)]
    candidates.append({'url':'https://docs.example','title':'Python documentation'})
    def read(url):
        opened.append(url)
        return {'url':url,'title':'Python documentation',
                'summary':'Python lists are mutable sequences whose contents can be changed in place.'}
    ResearchLoop('Python documentation about lists', search=lambda _:candidates, read=read,
                 budget=Budget(max_queries=1,max_sources_per_query=2)).run('Python lists')
    assert opened[0]=='https://docs.example'
    assert len(opened)==2


def test_the_loop_reads_several_sources_and_follows_up_once() -> None:
    pages = {
        "https://a.example": {
            "summary": "The course runs on Tuesday mornings. Registration closes on the fifth of September."
        },
        "https://b.example": {
            "summary": "The course runs on Tuesday mornings. Registration closes on the ninth of September."
        },
        "https://c.example": {
            "summary": "The examination period begins in the middle of December."
        },
    }
    searches: list[str] = []

    def search(query: str):
        searches.append(query)
        if len(searches) == 1:
            return [{"url": "https://a.example"}, {"url": "https://b.example"}]
        return [{"url": "https://c.example"}]

    task = TaskContext(goal="research")
    loop = ResearchLoop(
        "when does the course run",
        search=search,
        read=lambda url: pages[url],
        follow_up=lambda ledger: "examination dates" if len(ledger.queries) < 2 else None,
        budget=Budget(max_sources=8, max_queries=4, coverage_target=99),
        task=task,
    )

    report = loop.run()

    assert report["source_count"] == 3
    assert searches == ["course run", "examination dates"]
    # The corroborated fact collapsed; the conflicting one stayed apart.
    assert report["corroborated"] >= 1
    assert report["disputed"] == 2
    assert "research_source" in [event.kind for event in task.events]


def test_one_search_wave_has_a_focused_source_cap() -> None:
    pages = {
        f"https://source{index}.example": {
            "summary": f"The validated report contains finding number {index} for this question."
        }
        for index in range(6)
    }
    loop = ResearchLoop(
        "focused question",
        search=lambda query: [{"url": url} for url in pages],
        read=lambda url: pages[url],
        follow_up=lambda ledger: None,
        budget=Budget(
            max_sources=99,
            max_queries=4,
            max_sources_per_query=2,
            coverage_target=99,
        ),
    )

    report = loop.run()

    assert report["source_count"] == 2


def test_status_research_uses_fresh_official_then_version_focused_independent_query() -> None:
    searches: list[str] = []
    follow_ups: list[int] = []
    pages = {
        "https://python.example/3147": {
            "summary": "Python 3.14.7 is the current maintenance release today."
        },
        "https://technical.example/3147": {
            "summary": "Python 3.14.7 is the current maintenance release today."
        },
    }

    def search(query: str):
        searches.append(query)
        if "independent technical" in query:
            return [{"url": "https://technical.example/3147"}]
        return [{"url": "https://python.example/3147"}]

    def follow_up(ledger):
        follow_ups.append(len(ledger.queries))
        return "an unnecessary broad query"

    report = ResearchLoop(
        "Research the current Python 3.14 release status using official Python "
        "sources and at least one independent technical source. Prefer fewer "
        "validated sources and give direct citations.",
        search=search,
        read=lambda url: pages[url],
        follow_up=follow_up,
        budget=Budget(
            max_sources=10,
            max_queries=4,
            max_sources_per_query=2,
            coverage_target=1,
            validation_rounds=1,
        ),
    ).run()

    assert len(searches) == 2
    assert follow_ups == []
    assert searches[0].startswith("the current Python 3.14 release status")
    assert "as of " in searches[0]
    assert searches[0].endswith("official authoritative source")
    assert "3.14.7" in searches[1]
    assert searches[1].endswith("independent technical source confirmation")
    assert report["relevant_publisher_count"] == 2
    assert report["evidence_sufficient"] is True
    assert report["stop_reason"] == "evidence_sufficient"


def test_a_stopped_research_task_stops_retrieving() -> None:
    task = TaskContext(goal="research")
    task.request_stop("user_requested")
    loop = ResearchLoop(
        "anything",
        search=lambda query: [{"url": "https://a.example"}],
        read=lambda url: {"summary": "Something interesting happened here today."},
        task=task,
    )

    report = loop.run()

    assert report["stop_reason"] == "cancelled"
    assert report["source_count"] == 0


def test_a_source_that_will_not_load_does_not_end_the_research() -> None:
    def read(url: str):
        if url == "https://broken.example":
            raise RuntimeError("connection reset")
        return {"summary": "The department enrolled twelve thousand students in 2026."}

    loop = ResearchLoop(
        "enrolment",
        search=lambda query: [
            {"url": "https://broken.example"},
            {"url": "https://good.example"},
        ],
        read=read,
        budget=Budget(coverage_target=99),
    )

    report = loop.run()

    assert report["claim_count"] == 1


def test_invalid_or_expired_destination_is_rejected_not_counted_as_evidence() -> None:
    pages = {
        "https://broken.example": {
            "title": "Invite Invalid",
            "summary": "Invite Invalid. This invite may be expired, or you may not have permission to join.",
        },
        "https://good.example": {
            "title": "Current report",
            "summary": "The department enrolled twelve thousand students during the current academic year.",
        },
    }
    loop = ResearchLoop(
        "current enrolment",
        search=lambda query: [{"url": url} for url in pages],
        read=lambda url: pages[url],
        budget=Budget(coverage_target=99),
    )

    report = loop.run()

    assert report["source_count"] == 1
    assert report["sources"][0]["url"] == "https://good.example"
    assert report["rejected_sources"][0]["url"] == "https://broken.example"
    assert "invalid page" in report["rejected_sources"][0]["reason"]


def test_browser_challenge_is_rejected_not_counted_as_research_evidence() -> None:
    loop = ResearchLoop(
        "current release",
        search=lambda query: [{"url": "https://challenge.example/release"}],
        read=lambda url: {
            "title": "Just a moment...",
            "summary": (
                "Just a moment. Please enable JavaScript and cookies to continue "
                "while we perform a security verification."
            ),
        },
        budget=Budget(coverage_target=99),
    )

    report = loop.run()

    assert report["source_count"] == 0
    assert report["rejected_sources"][0]["url"] == "https://challenge.example/release"


def test_research_checkpoint_resumes_without_rereading_validated_pages(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "research.json"
    pages = {
        "https://a.example": {
            "summary": "The course begins on the first day of September this academic year."
        },
        "https://b.example": {
            "summary": "The course begins on the first day of September this academic year."
        },
    }
    first_task = TaskContext(goal="research")
    reads: list[str] = []

    def first_read(url: str):
        reads.append(url)
        first_task.request_stop("simulate_restart")
        return pages[url]

    first = ResearchLoop(
        "when does the course begin",
        search=lambda query: [{"url": url} for url in pages],
        read=first_read,
        task=first_task,
        budget=Budget(coverage_target=99),
        checkpoint_path=checkpoint,
    ).run()
    assert first["stop_reason"] == "cancelled"
    assert checkpoint.is_file()

    second = ResearchLoop(
        "when does the course begin",
        search=lambda query: [{"url": url} for url in pages],
        read=lambda url: reads.append(url) or pages[url],
        budget=Budget(coverage_target=99),
        checkpoint_path=checkpoint,
    ).run()

    assert second["source_count"] == 2
    assert reads.count("https://a.example") == 1
    assert reads.count("https://b.example") == 1


# ----------------------------------------------------------------- scheduler


def test_a_schedule_must_say_when_it_runs() -> None:
    scheduler = Scheduler()
    with pytest.raises(ScheduleError, match="interval"):
        scheduler.add(name="x", goal="y", trigger=TRIGGER_RECURRING)
    with pytest.raises(ScheduleError, match="event name"):
        scheduler.add(name="x", goal="y", trigger=TRIGGER_EVENT)
    with pytest.raises(ScheduleError, match="not a trigger"):
        scheduler.add(name="x", goal="y", trigger="whenever")


def test_only_due_work_runs() -> None:
    scheduler = Scheduler()
    soon = scheduler.add(name="soon", goal="scan", trigger=TRIGGER_NOW)
    scheduler.add(
        name="later", goal="scan", trigger=TRIGGER_RECURRING, interval_seconds=3600
    )

    due = scheduler.due()

    assert [task.task_id for task in due] == [soon.task_id]
    # Something is due right now, so there is nothing to wait for.
    assert scheduler.next_due_in() == 0.0

    # With the due one gone, the runner sleeps until the next is actually due
    # rather than waking to find nothing to do.
    scheduler.remove(soon.task_id)
    assert 0 < scheduler.next_due_in() <= 3600


def test_a_recurring_job_reschedules_itself() -> None:
    scheduler = Scheduler()
    task = scheduler.add(
        name="inbox", goal="scan", trigger=TRIGGER_RECURRING, interval_seconds=60
    )
    task.next_run_at = time.time() - 1

    scheduler.run_due(lambda _task: {"state": "completed"})

    assert task.last_result == "completed"
    assert task.next_run_at > time.time()
    assert task.due() is False


def test_a_failing_job_backs_off_instead_of_hammering() -> None:
    scheduler = Scheduler()
    task = scheduler.add(
        name="broken", goal="scan", trigger=TRIGGER_RECURRING, interval_seconds=1
    )
    task.next_run_at = time.time() - 1

    def failing(_task):
        raise RuntimeError("the service is down")

    scheduler.run_due(failing)

    assert task.consecutive_failures == 1
    assert task.last_result == "failed"
    # Next attempt is a minute away, not a second.
    assert task.next_run_at - time.time() > 30


def test_a_job_that_keeps_failing_is_parked_rather_than_retried_forever() -> None:
    scheduler = Scheduler()
    task = scheduler.add(
        name="broken", goal="scan", trigger=TRIGGER_RECURRING, interval_seconds=1
    )

    for _ in range(6):
        task.next_run_at = time.time() - 1
        scheduler.run_due(lambda _t: (_ for _ in ()).throw(RuntimeError("down")))

    assert task.exhausted is True
    assert task.due() is False
    # Re-enabling forgives the backoff, because the user has presumably fixed it.
    scheduler.enable(task.task_id)
    assert task.exhausted is False


def test_waiting_for_the_user_is_not_counted_as_a_failure() -> None:
    scheduler = Scheduler()
    task = scheduler.add(name="send", goal="send report", trigger=TRIGGER_NOW)

    scheduler.run_due(lambda _task: {"state": "waiting"})

    # A workflow that stopped to ask permission has not failed and must not
    # accumulate backoff as though it had.
    assert task.consecutive_failures == 0
    assert task.last_result == "waiting"


def test_an_event_wakes_the_work_that_was_waiting_for_it() -> None:
    scheduler = Scheduler()
    task = scheduler.add(
        name="resume", goal="continue", trigger=TRIGGER_EVENT, event_name="signed_in"
    )
    assert scheduler.due() == []

    woken = scheduler.fire_event("signed_in", {"account": "anik"})

    assert [item.task_id for item in woken] == [task.task_id]
    assert [item.task_id for item in scheduler.due()] == [task.task_id]
    assert task.payload["account"] == "anik"


def test_background_work_gets_no_extra_authority(tmp_path: Path) -> None:
    """A scheduled send waits for a person exactly as an interactive one does."""

    from app.backend.connectors import ApprovalRequired

    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(LocalMailConnector())
    scheduler = Scheduler()
    task = scheduler.add(name="digest", goal="send the digest", trigger=TRIGGER_NOW)
    outcomes: list[str] = []

    def runner(scheduled):
        try:
            registry.invoke("mail.local", "send", {"to": "a@b.test", "subject": "digest"})
        except ApprovalRequired:
            outcomes.append("waiting")
            return {"state": "waiting"}
        outcomes.append("sent")
        return {"state": "completed"}

    scheduler.run_due(runner)

    assert outcomes == ["waiting"]
    # Nothing was sent while nobody was watching.
    assert registry.invoke("mail.local", "list_sent", {}).data["count"] == 0


def test_schedules_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    first = Scheduler(path=path)
    created = first.add(
        name="inbox", goal="scan", trigger=TRIGGER_RECURRING, interval_seconds=900
    )

    second = Scheduler(path=path)

    restored = second.get(created.task_id)
    assert restored is not None
    assert restored.name == "inbox"
    assert restored.interval_seconds == 900
