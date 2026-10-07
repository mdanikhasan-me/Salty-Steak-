"""Mail triage: the deterministic half, proven without the model.

These use a scripted `generate` so the batching, grouping, conservatism and
inheritance rules are tested as software. Whether Base Steak's *judgement* is
good is a separate, live question measured against the fixture mailboxes.
"""

from __future__ import annotations

import json

from app.backend.mail import Message, classify, features, group_repeats
from tests.fixtures.mailboxes import freelancer_mailbox, score, student_mailbox


def _reply(entries):
    return json.dumps({"verdicts": entries})


def test_features_describe_shape_and_never_decide() -> None:
    promotional = Message(
        identifier="a",
        sender="offers@shop.test",
        subject="50% off",
        snippet="Unsubscribe at any time",
        headers={"List-Unsubscribe": "<x>"},
    )
    signals = features(promotional)
    assert signals["list_mail"] and signals["bulk_language"]

    # A receipt wears the same costume, which is exactly why a signal is not a
    # verdict: both reach the model, and the model decides.
    receipt = Message(
        identifier="b",
        sender="no-reply@shop.test",
        subject="Your order has shipped",
        snippet="Track your parcel. Unsubscribe at any time",
        headers={"List-Unsubscribe": "<x>"},
    )
    assert features(receipt)["list_mail"] and features(receipt)["bulk_language"]


def test_near_identical_messages_cost_one_decision() -> None:
    messages = [
        Message(identifier=f"m{index}", sender="offers@shop.test", subject="Sale 70% off!")
        for index in range(5)
    ] + [Message(identifier="other", sender="person@mail.test", subject="lunch?")]

    representatives, followers = group_repeats(messages)

    assert len(representatives) == 2
    assert sum(len(items) for items in followers.values()) == 4


def test_a_message_the_model_ignored_is_never_swept_up() -> None:
    messages = [
        Message(identifier="a", sender="offers@shop.test", subject="Sale"),
        Message(identifier="b", sender="bank@bank.test", subject="Statement"),
    ]
    # The model answers about one message and forgets the other entirely.
    report = classify(
        messages,
        generate=lambda _: _reply([{"id": "a", "category": "promotion", "low_value": True}]),
    )

    assert report.verdicts["a"].actionable
    assert not report.verdicts["b"].actionable
    assert report.verdicts["b"].category == "uncertain"


def test_uncertainty_survives_into_the_result_rather_than_becoming_action() -> None:
    messages = [Message(identifier="a", sender="x@y.test", subject="Something")]
    report = classify(
        messages,
        generate=lambda _: _reply([{"id": "a", "category": "uncertain", "low_value": True}]),
    )

    # low_value with an uncertain category must not be actionable.
    assert not report.verdicts["a"].actionable
    assert report.uncertain


def test_only_the_unsure_messages_have_their_content_fetched() -> None:
    messages = [
        Message(identifier="a", sender="offers@shop.test", subject="Sale"),
        Message(identifier="b", sender="odd@thing.test", subject="Hmm"),
    ]
    fetched: list[list[str]] = []

    replies = iter(
        [
            _reply(
                [
                    {"id": "a", "category": "promotion", "low_value": True},
                    {"id": "b", "category": "uncertain", "low_value": False},
                ]
            ),
            _reply([{"id": "b", "category": "newsletter", "low_value": True}]),
        ]
    )

    def fetch(ids):
        fetched.append(list(ids))
        return {identifier: "full text" for identifier in ids}

    report = classify(messages, generate=lambda _: next(replies), fetch_body=fetch)

    # Content was pulled for the uncertain message only.
    assert fetched == [["b"]]
    assert report.resolved_uncertain == 1
    assert report.verdicts["b"].actionable


def test_grouping_never_widens_what_gets_acted_on() -> None:
    messages = [
        Message(identifier="m1", sender="offers@shop.test", subject="Sale 70% off"),
        Message(identifier="m2", sender="offers@shop.test", subject="Sale 70% off"),
    ]
    # The representative is uncertain, so its duplicate must be too.
    report = classify(
        messages,
        generate=lambda _: _reply([{"id": "m1", "category": "uncertain", "low_value": False}]),
    )

    assert not any(verdict.actionable for verdict in report.verdicts.values())


def test_a_mailbox_costs_batches_not_one_call_per_message() -> None:
    messages, _ = student_mailbox()
    report = classify(
        messages,
        generate=lambda _: _reply([]),
        batch_size=25,
    )

    # One call for the whole batch, plus one resolving pass.
    assert report.model_calls <= 2
    assert report.examined == len(messages)


def test_the_scorer_separates_the_two_kinds_of_mistake() -> None:
    messages, truth = freelancer_mailbox()
    # A classifier that reads "bulk" as "junk" sweeps up the payout notice.
    report = classify(
        messages,
        generate=lambda _: _reply(
            [
                {"id": identifier, "category": "promotion", "low_value": True}
                for identifier in ("f1", "f2", "f4", "f5")
            ]
        ),
    )
    result = score(report.verdicts, truth)

    # f1 and f2 are a paid invoice and a payout: the mistakes that matter.
    assert result["false_positives"] == 2
    assert set(result["false_positive_ids"]) == {"f1", "f2"}
