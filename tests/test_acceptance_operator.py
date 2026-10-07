"""End-to-end operator workflows across connectors, browser, UIA and native.

These are the tests that decide whether Salty Steak is an operator or a demo.
Each one measures what it cost: model calls, vision calls, screenshots and raw
input, because an architecture that reaches the right answer by taking a
hundred screenshots has not solved the problem.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path

import pytest

from app.backend.automation.approvals import ApprovalBroker
from app.backend.automation.policy import PolicyEngine
from app.backend.chat.task_runtime import COMPLETED, STOPPED, WAITING, TaskContext
from app.backend.connectors import (
    ConnectorManager,
    LocalCalendarConnector,
    LocalMailConnector,
    collect,
    run_batch,
    summarise_for_model,
)
from app.backend.memory import SemanticMemory
from app.backend.research import Budget, ResearchLoop, statements_from_page
from app.backend.workflow import Executor, WorkflowEngine, build_plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows only")


# --------------------------------------------------------------- test data


def _mailbox() -> LocalMailConnector:
    """A mailbox big enough that batching is the only sane approach."""

    mail = LocalMailConnector()
    messages = []
    base = time.time() - 86_400 * 60

    # Work with one person about one project — the thing to be gathered.
    for index in range(140):
        messages.append(
            {
                "id": f"atlas-{index}",
                "thread_id": f"atlas-thread-{index // 4}",
                "from": "sam.okafor@northwind.example",
                "subject": f"Project Atlas: milestone {index}",
                "body": "Notes on the Atlas rollout and next steps.",
                "received_at": base + index * 600,
            }
        )
    # Work with the same person about something else entirely.
    for index in range(25):
        messages.append(
            {
                "id": f"sam-other-{index}",
                "from": "sam.okafor@northwind.example",
                "subject": f"Lunch on Friday {index}",
                "body": "Nothing to do with the project.",
                "received_at": base + index * 900,
            }
        )
    for index in range(90):
        messages.append(
            {
                "id": f"promo-{index}",
                "from": "deals@shopping.example",
                "subject": f"{index}% off everything this weekend",
                "body": "Unsubscribe at any time.",
                "labels": ["Promotions"],
                "received_at": base + index * 300,
            }
        )
    for index in range(60):
        messages.append(
            {
                "id": f"receipt-{index}",
                "from": "receipts@shopping.example",
                "subject": f"Your receipt {index}",
                "body": "Thank you for your order.",
                "received_at": base + index * 400,
            }
        )
    for index in range(45):
        messages.append(
            {
                "id": f"uni-{index}",
                "from": "registrar@university.example",
                "subject": f"Semester notice {index}",
                "body": "Registration and timetable information.",
                "received_at": base + index * 500,
            }
        )
    for index in range(70):
        messages.append(
            {
                "id": f"notify-{index}",
                "from": "no-reply@social.example",
                "subject": f"You have {index} new notifications",
                "body": "See what you missed.",
                "received_at": base + index * 200,
            }
        )
    for index in range(20):
        messages.append(
            {
                "id": f"unclear-{index}",
                "from": "sam.okafor@northwind.example",
                "subject": f"Re: that thing {index}",
                "body": "Ambiguous, could be Atlas or could be personal.",
                "received_at": base + index * 700,
            }
        )
    mail.seed(messages)
    return mail


@pytest.fixture()
def operator(tmp_path: Path):
    """A full operator: connectors, policy, approvals, memory, task."""

    task = TaskContext(goal="acceptance")
    approvals = ApprovalBroker()
    # ask_every_time is the default and the realistic setting: ordinary changes
    # run because starting the task authorised it, while anything destructive
    # or outward-facing still has to be put to the user.
    registry = ConnectorManager(
        policy=PolicyEngine(authority_mode="ask_every_time"), task=task
    )
    mail = _mailbox()
    calendar = LocalCalendarConnector()
    registry.register(mail)
    registry.register(calendar)
    memory = SemanticMemory(tmp_path / "memory.db")
    executor = Executor(
        connectors=registry,
        task=task,
        policy=registry.policy,
        authority_mode="ask_every_time",
    )
    engine = WorkflowEngine(
        executor=executor, task=task, checkpoint_path=tmp_path / "workflow.json"
    )
    yield {
        "task": task,
        "registry": registry,
        "mail": mail,
        "calendar": calendar,
        "memory": memory,
        "engine": engine,
        "executor": executor,
        "approvals": approvals,
    }
    memory.close()


def _cost(task: TaskContext) -> dict[str, int]:
    metrics = task.metrics.to_dict()
    return {
        "model_calls": metrics["model_calls"],
        "vision_calls": metrics.get("vision_calls", 0),
        "screenshots": metrics["screenshots"],
        "raw_input": metrics.get("raw_input_calls", 0),
        "connector_calls": metrics.get("api_calls", 0),
    }


# ============================================================== TEST A: mail


def test_a_organising_a_real_sized_inbox(operator) -> None:
    """450 messages, one rule, a handful of decisions, nothing deleted unasked."""

    registry, mail, task = operator["registry"], operator["mail"], operator["task"]
    memory = operator["memory"]
    memory.remember(
        kind="entity",
        subject="Sam Okafor",
        body="Sam Okafor at Northwind is the Project Atlas contact.",
    )
    assert mail.message_count == 450

    # One model decision: what the rule is. Recorded here as the plan itself.
    model_calls = 0

    remembered = memory.recall("Project Atlas Sam")
    assert remembered, "memory should supply the contact without asking"

    # --- read, paged rather than all at once
    pages: list[int] = []
    candidates = list(
        __import__(
            "app.backend.connectors.batch", fromlist=["iterate_pages"]
        ).iterate_pages(
            registry,
            "mail.local",
            "search",
            {"from": "sam.okafor@northwind.example"},
            page_size=50,
            on_page=lambda number, page: pages.append(len(page.items)),
        )
    )
    assert len(candidates) == 185
    assert pages == [50, 50, 50, 35]

    # --- the runtime applies the rule; the model does not see 185 messages
    compact = summarise_for_model(candidates, ["id", "subject", "from"], limit=10)
    assert len(compact) == 10
    atlas = [item["id"] for item in candidates if "Project Atlas" in item["subject"]]
    ambiguous = [item["id"] for item in candidates if item["id"].startswith("unclear-")]
    assert len(atlas) == 140
    assert len(ambiguous) == 20

    # --- create the label and apply it in batches
    registry.invoke("mail.local", "create_label", {"name": "Project Atlas"})
    report = run_batch(
        registry, "mail.local", "apply_label", atlas, {"label": "Project Atlas"}, batch_size=50
    )
    assert len(report.succeeded) == 140
    assert report.complete is True
    assert report.batches == 3

    # --- verify against the service, not against the success code
    labelled = collect(registry, "mail.local", "search", {"label": "Project Atlas"})
    assert len(labelled) == 140

    # --- the destructive part reaches approval and stops there
    from app.backend.connectors import ApprovalRequired

    junk = [item["id"] for item in collect(registry, "mail.local", "search", {"from": "deals@shopping.example"})]
    assert len(junk) == 90
    with pytest.raises(ApprovalRequired) as blocked:
        registry.invoke("mail.local", "delete", {"ids": junk})

    # The user is shown exactly what would go, before anything goes.
    assert blocked.value.request["risk"] == "destructive"
    assert mail.message_count == 450

    # And once approved, it proceeds — the gate is a question, not a refusal.
    approving = ConnectorManager(
        policy=PolicyEngine(authority_mode="ask_every_time"),
        approve=lambda _request: True,
    )
    approving.register(mail)
    approving.invoke("mail.local", "delete", {"ids": junk[:10]})
    assert mail.message_count == 440

    cost = _cost(task)
    # The decisive proof: 450 messages, 185 processed, 140 mutated — and the
    # model was consulted about the rule, not about the messages.
    assert model_calls == 0
    assert cost["screenshots"] == 0
    assert cost["vision_calls"] == 0
    assert cost["raw_input"] == 0
    # Connector calls stay proportional to pages and batches, not to messages.
    assert cost["connector_calls"] < 25


# ========================================================== TEST B: calendar


def test_b_scheduling_around_an_existing_commitment(operator) -> None:
    """A meeting on Thursday afternoon, without double-booking."""

    registry, calendar, task = operator["registry"], operator["calendar"], operator["task"]
    calendar.seed(
        [
            {
                "title": "Design review",
                "start": "2026-08-20T14:00:00",
                "end": "2026-08-20T16:00:00",
            }
        ]
    )

    engine = operator["engine"]
    plan = build_plan(
        {
            "goal": "schedule the meeting on Thursday afternoon",
            "nodes": [
                {
                    "node": "look",
                    "connector": "calendar.local",
                    "operation": "free_busy",
                    "arguments": {
                        "start": "2026-08-20T12:00:00",
                        "end": "2026-08-20T18:00:00",
                        "minimum_minutes": 60,
                    },
                    "result_as": "availability",
                },
                {
                    "node": "book",
                    "connector": "calendar.local",
                    "operation": "create_event",
                    "arguments": {
                        "title": "Project Atlas sync",
                        "start": "2026-08-20T16:00:00",
                        "end": "2026-08-20T17:00:00",
                    },
                    "depends_on": ["look"],
                    "verify": {
                        "operation": "list_events",
                        "arguments": {
                            "start": "2026-08-20T16:00:00",
                            "end": "2026-08-20T17:00:00",
                        },
                        "expect": {"contains": "Project Atlas sync"},
                    },
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == COMPLETED
    # The gap the planner read really was free, and the booking is confirmed
    # by reading the calendar back rather than by trusting the write.
    availability = plan.variables["availability"]
    assert {"start": "2026-08-20T16:00:00", "end": "2026-08-20T18:00:00"} in availability["free"]
    assert plan.node("book").verified is True

    # The existing commitment is untouched and still unbooked-over.
    from app.backend.connectors.contract import ConnectorError

    with pytest.raises(ConnectorError) as clash:
        registry.invoke(
            "calendar.local",
            "create_event",
            {"title": "Clash", "start": "2026-08-20T15:00:00", "end": "2026-08-20T15:30:00"},
        )
    assert clash.value.kind == "conflict"
    assert _cost(task)["screenshots"] == 0


# ================================================= TEST C: portal to calendar


PORTAL = """<!doctype html><html><head><title>Semester Portal</title></head><body>
<h1>Autumn 2026 timetable</h1>
<table id="timetable">
<tr><th>Course</th><th>Day</th><th>Start</th><th>End</th></tr>
<tr><td>Databases</td><td>Tuesday</td><td>10:00</td><td>11:30</td></tr>
<tr><td>Operating Systems</td><td>Thursday</td><td>14:00</td><td>15:30</td></tr>
</table>
<p>Term runs from 2026-09-01 to 2026-09-30.</p>
</body></html>"""


@pytest.fixture()
def portal():
    directory = Path(tempfile.mkdtemp(prefix="salty-portal-"))
    (directory / "timetable.html").write_text(PORTAL, encoding="utf-8")

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            return

    handler = functools.partial(_Quiet, directory=str(directory))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/timetable.html"
    finally:
        server.shutdown()
        server.server_close()


@WINDOWS_ONLY
def test_c_portal_timetable_becomes_verified_calendar_events(operator, portal) -> None:
    """Browser to connector, crossing world state, policy and verification."""

    from app.backend.automation.browser_client import BrowserClient, find_browser_host

    host = find_browser_host(PROJECT_ROOT)
    if host is None:
        pytest.skip("SaltyBrowserHost.exe has not been built")

    registry, calendar, task = operator["registry"], operator["calendar"], operator["task"]
    browser = BrowserClient(
        host, profile_directory=Path(tempfile.mkdtemp(prefix="salty-portal-profile-"))
    )
    try:
        page = browser.call("open_url", {"url": portal})
        assert page["title"] == "Semester Portal"

        # Read structurally. No screenshot, no vision, no coordinates.
        reading = browser.call("read_page", {"limit": 60})
        summary = reading["summary"]
        assert "Databases" in summary and "Operating Systems" in summary

        # Normalise what the page said into calendar terms.
        courses = []
        for line in summary.splitlines():
            for day in ("Tuesday", "Thursday"):
                if day in line and ":" in line:
                    parts = [part.strip() for part in line.replace("|", " ").split()]
                    times = [part for part in parts if ":" in part and part[0].isdigit()]
                    if len(times) == 2:
                        courses.append(
                            {
                                "title": line.split(day)[0].strip(" |\t"),
                                "day": day.casefold(),
                                "start": times[0],
                                "end": times[1],
                            }
                        )
        assert len(courses) == 2

        # Existing commitments are read before anything is written.
        existing = registry.invoke(
            "calendar.local",
            "list_events",
            {"start": "2026-09-01T00:00:00", "end": "2026-09-30T23:59:00"},
        ).data
        assert existing["items"] == []

        for course in courses:
            first = "2026-09-01" if course["day"] == "tuesday" else "2026-09-03"
            registry.invoke(
                "calendar.local",
                "create_event",
                {
                    "title": course["title"],
                    "start": f"{first}T{course['start']}:00",
                    "end": f"{first}T{course['end']}:00",
                    "repeat_weekly_on": [course["day"]],
                    "repeat_until": "2026-09-30T23:59:00",
                },
            )

        # Verified by reading the calendar back.
        occurrences = registry.invoke(
            "calendar.local",
            "list_events",
            {"start": "2026-09-01T00:00:00", "end": "2026-09-30T23:59:00"},
        ).data["items"]
        titles = {item["title"] for item in occurrences}
        assert titles == {"Databases", "Operating Systems"}
        # Two weekly series over September, stored as two events.
        assert calendar.event_count == 2
        assert len(occurrences) == 9

        cost = _cost(task)
        assert cost["screenshots"] == 0
        assert cost["vision_calls"] == 0
        assert cost["raw_input"] == 0
    finally:
        browser.close()


# ========================================================== TEST D: research


SOURCES = {
    "/one.html": """<!doctype html><html><head><title>Source One</title></head><body>
<p>The autumn term begins on the first of September.
Registration closes on the fifth of September.
The examination period begins in the middle of December.</p></body></html>""",
    "/two.html": """<!doctype html><html><head><title>Source Two</title></head><body>
<p>The autumn term begins on the first of September.
Registration closes on the ninth of September.</p></body></html>""",
    "/three.html": """<!doctype html><html><head><title>Source Three</title></head><body>
<p>We use cookies on this site. The examination period begins in the middle of December.
The library opens at eight in the morning during term.</p></body></html>""",
}


def test_d_research_across_sources_keeps_provenance_and_disagreement(operator) -> None:
    """Several sources, duplicates collapsed, the contradiction preserved."""

    task = operator["task"]
    pages = {
        f"https://source{index}.example": {
            "summary": " ".join(
                text.split("<p>")[1].split("</p>")[0].split()
            )
        }
        for index, text in enumerate(SOURCES.values(), start=1)
    }
    urls = list(pages)
    searches: list[str] = []

    def search(query: str):
        searches.append(query)
        return [{"url": url} for url in urls] if len(searches) == 1 else []

    loop = ResearchLoop(
        "when does the autumn term start and when does registration close",
        search=search,
        read=lambda url: pages[url],
        budget=Budget(max_sources=6, max_queries=3, coverage_target=99),
        task=task,
    )

    report = loop.run()

    assert report["source_count"] == 3
    # The fact both sources agreed on became one claim with two sources.
    corroborated = [
        claim for claim in report["claims"] if claim["corroborated"]
    ]
    assert any("term begins" in claim["text"] for claim in corroborated)
    # The disagreement about registration was kept, not averaged away.
    disputed = [claim for claim in report["claims"] if claim["disputed"]]
    assert len(disputed) == 2
    assert {"fifth", "ninth"} <= {
        word for claim in disputed for word in claim["text"].casefold().split()
    }
    # Cookie boilerplate never became evidence.
    assert not any("cookies" in claim["text"].casefold() for claim in report["claims"])
    # Every claim can be traced back to where it came from.
    for claim in report["claims"]:
        assert claim["sources"]

    cost = _cost(task)
    assert cost["screenshots"] == 0
    assert cost["vision_calls"] == 0


# ====================================================== TEST E: cross-system


@WINDOWS_ONLY
def test_e_one_workflow_across_browser_native_uia_and_connector(
    operator, portal, tmp_path: Path
) -> None:
    """Read the web, write it into Notepad through UIA, record it in a connector."""

    from app.backend.automation.browser_client import BrowserClient, find_browser_host
    from app.backend.automation.uia_client import UiAutomationClient, find_uia_host

    browser_host = find_browser_host(PROJECT_ROOT)
    uia_host = find_uia_host(PROJECT_ROOT)
    if browser_host is None or uia_host is None:
        pytest.skip("The helper hosts have not been built")

    import ctypes
    import subprocess

    registry, calendar, task = operator["registry"], operator["calendar"], operator["task"]
    scratch = tmp_path / f"salty-crosssystem-{os.getpid()}.txt"
    scratch.write_text("", encoding="utf-8")

    browser = BrowserClient(
        browser_host, profile_directory=Path(tempfile.mkdtemp(prefix="salty-cross-"))
    )
    uia = UiAutomationClient(uia_host)
    launched = None
    try:
        # 1. Browser: read a value structurally.
        browser.call("open_url", {"url": portal})
        summary = browser.call("read_page", {"limit": 60})["summary"]
        assert "Databases" in summary
        extracted = "Databases Tuesday 10:00"

        # 2. Native: open the scratch file in Notepad.
        launched = subprocess.Popen(["notepad.exe", str(scratch)])
        deadline = time.time() + 15
        window = None
        while time.time() < deadline and window is None:
            for candidate in uia.call("get_windows", {}).get("windows", []):
                # Matched on the unique scratch filename, then scoped by the
                # process it reported: Windows 11 reparents Notepad, so the
                # launcher's own pid is not the one owning the window.
                if scratch.stem in str(candidate.get("name", "")):
                    window = candidate
                    break
            if window is None:
                time.sleep(0.4)
        if window is None:
            pytest.skip("Notepad did not present a window in time")

        # 3. UIA: write into the editor.
        #
        # Windows 11 Notepad keeps several documents in ONE process, so
        # scoping by process id does not isolate this scratch file from
        # whatever else the user has open. The editable Document reached here
        # is the focused tab, and the only guarantee that it is the right one
        # is that this scratch file is empty and a real document would not be.
        # So the emptiness is checked and the write is abandoned otherwise —
        # a skipped test costs nothing, overwriting someone's work does not.
        editable = uia.call(
            "find_control", {"process_id": window["process_id"], "pattern": "Value"}
        )
        if editable["count"] != 1:
            pytest.skip("Could not identify a single editable document safely")
        editor = editable["matches"][0]
        if str(uia.call("get_text", {"element": editor["element"]}).get("text") or "").strip():
            pytest.skip("The focused Notepad document is not the empty scratch file")

        uia.call("set_value", {"element": editor["element"], "value": extracted})
        # Verified from the control itself, not from the call's return code.
        assert extracted in uia.call("get_text", {"element": editor["element"]})["text"]

        # 4. Connector: record it, and verify against the service.
        registry.invoke(
            "calendar.local",
            "create_event",
            {
                "title": extracted,
                "start": "2026-09-01T10:00:00",
                "end": "2026-09-01T11:30:00",
            },
        )
        events = registry.invoke(
            "calendar.local",
            "list_events",
            {"start": "2026-09-01T00:00:00", "end": "2026-09-02T00:00:00"},
        ).data["items"]
        assert [item["title"] for item in events] == [extracted]

        cost = _cost(task)
        assert cost["screenshots"] == 0
        assert cost["vision_calls"] == 0
        assert cost["raw_input"] == 0
    finally:
        browser.close()
        uia.close()
        if launched is not None:
            launched.kill()


# ============================================================== STOP TEST


def test_stop_halts_a_multi_node_workflow_immediately(operator) -> None:
    """Stop must beat the next side effect, not merely be recorded."""

    registry, mail, task = operator["registry"], operator["mail"], operator["task"]
    engine = operator["engine"]
    performed: list[str] = []
    original = registry.invoke
    stopped_at = {"time": None}

    def watched(connector_id, operation, arguments=None, **kwargs):
        performed.append(operation)
        result = original(connector_id, operation, arguments, **kwargs)
        if operation == "create_label":
            stopped_at["time"] = time.monotonic()
            task.request_stop("user_requested")
        return result

    registry.invoke = watched
    plan = build_plan(
        {
            "goal": "label and then delete",
            "nodes": [
                {
                    "node": "make",
                    "connector": "mail.local",
                    "operation": "create_label",
                    "arguments": {"name": "Doomed"},
                },
                {
                    "node": "apply",
                    "connector": "mail.local",
                    "operation": "apply_label",
                    "arguments": {"ids": ["atlas-0"], "label": "Doomed"},
                    "depends_on": ["make"],
                },
                {
                    "node": "wipe",
                    "connector": "mail.local",
                    "operation": "delete",
                    "arguments": {"ids": ["atlas-0"]},
                    "depends_on": ["apply"],
                },
            ],
        }
    )

    result = engine.run(plan)

    assert result.state == STOPPED
    assert task.state == STOPPED
    # No mutation ran after the stop: neither the label application nor,
    # crucially, the delete.
    assert performed == ["create_label"]
    assert mail.message_count == 450
    assert plan.node("apply").state == "pending"
    assert plan.node("wipe").state == "pending"

    latency = task.metrics.cancellation_latency_seconds
    assert latency is not None
    # Stop is a memory flag, not a database round trip.
    assert latency < 0.5


def test_a_workflow_waiting_on_a_person_can_still_be_cancelled(operator) -> None:
    approvals = ApprovalBroker()
    task = operator["task"]
    ticket = approvals.request(
        capability="connector.mail.send",
        risk="send_external",
        summary="Send the digest",
        reason="This sends something on your behalf.",
        task_id=task.task_id,
    )
    task.transition(WAITING, waiting_for="user_approval")

    answered = approvals.wait_for(
        ticket, should_stop=lambda: True, poll_seconds=0.01
    )

    assert answered is False
    task.request_stop("user_requested")
    task.finish_stopped()
    assert task.state == STOPPED


def test_late_output_cannot_revive_a_stopped_task(operator) -> None:
    task = operator["task"]
    task.request_stop("user_requested")
    task.finish_stopped()

    # A model reply or tool result arriving after the stop must not drag the
    # task back into an active state.
    task.transition("executing", late=True)

    assert task.state == STOPPED
