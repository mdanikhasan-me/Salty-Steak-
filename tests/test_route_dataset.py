from collections import Counter

from app.backend.training.route_dataset import (
    ROUTE_CODES,
    ROUTE_SYSTEM,
    holdout_examples,
    regression_examples,
    training_examples,
)


def test_routing_post_training_dataset_is_balanced_and_disjoint() -> None:
    training = training_examples()
    holdout = holdout_examples()

    assert len(training) == 1100
    assert len(holdout) == 200
    assert Counter(value.response for value in training) == Counter(
        {code: 220 for code in ROUTE_CODES.values()}
    )
    assert Counter(value.response for value in holdout) == Counter(
        {code: 40 for code in ROUTE_CODES.values()}
    )
    assert {value.id for value in training}.isdisjoint(
        {value.id for value in holdout}
    )
    assert all(value.messages[0] == ("system", ROUTE_SYSTEM) for value in training)
    assert any(
        value.messages[-1] == ("user", "Reply with exactly the word ready.")
        and value.response == ROUTE_CODES["RESPOND"]
        for value in training
    )


def test_known_live_routing_regressions_cover_every_execution_family() -> None:
    regressions = regression_examples()

    assert len(regressions) == 20
    assert set(value.response for value in regressions) == set(ROUTE_CODES.values())
    assert any(
        value.messages[-1]
        == (
            "user",
            "Execute the approved command and return only the checked result.",
        )
        and value.response == ROUTE_CODES["AGENT"]
        for value in regressions
    )
    assert any(
        value.messages[-1] == ("user", "kamon acho?")
        and value.response == ROUTE_CODES["RESPOND"]
        for value in regressions
    )
    assert any(
        value.messages[-1] == ("user", "in md anik hasan what your name")
        and value.response == ROUTE_CODES["IDENTITY"]
        for value in regressions
    )


def test_routing_codes_are_single_distinct_ascii_symbols() -> None:
    assert set(ROUTE_CODES) == {
        "RESPOND",
        "RESEARCH",
        "IMAGE",
        "AGENT",
        "IDENTITY",
    }
    assert len(set(ROUTE_CODES.values())) == 5
    assert all(len(value) == 1 and value.isascii() for value in ROUTE_CODES.values())
