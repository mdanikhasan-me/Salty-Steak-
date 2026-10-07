from __future__ import annotations

from app.backend.research.activity import (
    MAX_VISIBLE_SOURCES,
    MIN_RESEARCH_ACTIVITY_ROWS,
    build_research_activity_journal,
    research_progress_snapshot,
)
from app.backend.research.ledger import Budget
from app.backend.research.loop import ResearchLoop
from app.backend.chat.runners import LiveRunners


def _snapshot(*, phase: str = "reading") -> dict:
    return {
        "phase": phase,
        "question": "What is the current Python 3.14 release?",
        "profile": "instant",
        "hard_ceiling_seconds": 300,
        "coverage_target": 6,
        "minimum_independent_sources": 2,
        "validation_rounds_required": 1,
        "validation_rounds_completed": 0,
        "query_count": 1,
        "source_count": 1,
        "claim_count": 4,
        "corroborated": 1,
        "independent_publisher_count": 1,
        "disputed": 0,
        "rejected_source_count": 0,
        "evidence_sufficient": False,
        "waves": [
            {
                "query": "Python 3.14 current release official source",
                "validation": False,
                "verified": 1,
                "rejected": 0,
                "state": "reading",
                "sites": [
                    {
                        "host": "python.org",
                        "url": "https://python.org/releases/3.14.7",
                        "title": "Python 3.14.7",
                        "state": "validated",
                    },
                    {
                        "host": "independent.example",
                        "url": "https://independent.example/python",
                        "title": "Release tracker",
                        "state": "reading",
                    },
                ],
            }
        ],
    }


def test_live_research_journal_has_a_truthful_public_minimum_and_current_source() -> None:
    journal = build_research_activity_journal(_snapshot())

    assert len(journal) >= MIN_RESEARCH_ACTIVITY_ROWS
    assert [entry["sequence"] for entry in journal] == list(
        range(1, len(journal) + 1)
    )
    assert any(entry["label"] == "Reading independent.example" for entry in journal)
    assert any(entry["state"] == "running" for entry in journal)
    assert any(entry["state"] == "pending" for entry in journal)


def test_completed_research_rewrites_the_journal_in_past_tense() -> None:
    report = {
        **_snapshot(phase="completed"),
        "source_count": 2,
        "independent_publisher_count": 2,
        "validation_rounds_completed": 1,
        "evidence_sufficient": True,
        "stop_reason": "evidence_sufficient",
        "status_target_version": "3.14.7",
        "status_target_publisher_count": 2,
    }
    snapshot = research_progress_snapshot(
        report,
        phase="completed",
        answer="Python 3.14.7 is current. [Source](https://python.org/releases/3.14.7)",
    )
    journal = build_research_activity_journal(snapshot)

    assert len(journal) >= MIN_RESEARCH_ACTIVITY_ROWS
    assert not {"pending", "running"}.intersection(
        entry["state"] for entry in journal
    )
    assert journal[0]["label"] == "Interpreted the research question"
    assert next(entry for entry in journal if entry["id"] == "research-answer")[
        "detail"
    ].startswith("Prepared 5 words")
    assert journal[-1]["detail"] == (
        "2 publishers confirmed 3.14.7 · 0 material disagreements · 0 rejected links"
    )


def test_multi_hour_activity_is_bounded_even_with_many_sources() -> None:
    snapshot = _snapshot()
    snapshot["waves"][0]["sites"] = [
        {
            "host": f"source-{index}.example",
            "url": f"https://source-{index}.example/report",
            "title": f"Report {index}",
            "state": "validated",
        }
        for index in range(100)
    ]
    snapshot["source_count"] = 100

    journal = build_research_activity_journal(snapshot)
    source_rows = [
        entry for entry in journal if entry["id"].startswith("research-source-")
    ]

    assert len(source_rows) == MAX_VISIBLE_SOURCES
    assert any(entry["id"] == "research-sources-earlier" for entry in journal)
    assert len(journal) < 50


def test_loop_publishes_the_page_it_is_about_to_read_and_retrieval_completion() -> None:
    progress: list[dict] = []

    def read(url: str) -> dict:
        current = progress[-1]
        assert current["phase"] == "reading"
        assert current["waves"][0]["sites"][0]["state"] == "reading"
        return {
            "url": url,
            "title": "Current report",
            "summary": "The current validated report contains a supported finding today.",
        }

    ResearchLoop(
        "current report",
        search=lambda _query: [{"url": "https://source.example/report"}],
        read=read,
        budget=Budget(
            coverage_target=1,
            min_independent_sources=1,
            validation_rounds=0,
        ),
        on_progress=lambda value: progress.append(dict(value)),
    ).run()

    assert "comparing" in [item["phase"] for item in progress]
    assert progress[-1]["phase"] == "retrieval_completed"
    assert progress[-1]["waves"][0]["state"] == "done"


def test_live_runner_streams_synthesis_draft_verification_and_completed_rewrite() -> None:
    progress: list[dict] = []

    def search(query: str) -> list[dict]:
        if "independent technical" in query:
            return [{"url": "https://tracker.example/python-3147"}]
        return [{"url": "https://python.example/python-3147"}]

    def read(url: str) -> dict:
        return {
            "url": url,
            "title": "Python 3.14.7 release status",
            "summary": "Python 3.14.7 is the current maintenance release today.",
        }

    def generate_with_preview(messages, on_preview) -> str:
        del messages
        on_preview(
            {
                "kind": "output",
                "tail_text": "Python 3.14.7 is current.",
                "token_count": 6,
                "character_count": 25,
            }
        )
        return "Python 3.14.7 is current."

    result = LiveRunners(
        search=search,
        read=read,
        generate=lambda _messages: '{"query":null}',
        generate_with_preview=generate_with_preview,
        research_profile="verification",
        on_research=lambda value: progress.append(dict(value)),
    ).run_research(
        decision={"question": "What is the current Python 3.14 release?"},
        request="What is the current Python 3.14 release?",
    )

    phases = [item["phase"] for item in progress]
    for expected in (
        "reading",
        "comparing",
        "retrieval_completed",
        "synthesizing",
        "drafting",
        "verifying",
        "completed",
    ):
        assert expected in phases
    drafting = next(
        item for item in progress if item["phase"] == "drafting" and item.get("generation_preview")
    )
    assert drafting["generation_preview"]["token_count"] == 6
    assert len(result["activity_journal"]) >= MIN_RESEARCH_ACTIVITY_ROWS
    assert not {"pending", "running"}.intersection(
        entry["state"] for entry in result["activity_journal"]
    )
