"""Stock is evidence, not an assumption.

"Unknown" is a real answer. Reading it as "in stock" is how a question about
what can be bought today gets answered with something nobody established was
buyable.
"""

from __future__ import annotations

from app.backend.chat.runners import _evidence_limits
from app.backend.research.pages import availability_of


def page(summary: str, **extra):
    return {"url": "https://shop.example.com/x", "title": "A drive", "summary": summary, **extra}


def test_structured_data_beats_prose() -> None:
    # A shop's own machine-readable offer is what its checkout uses.
    assert availability_of(page("In stock. Add to cart.", availability="https://schema.org/OutOfStock")) == "out_of_stock"
    assert availability_of(page("", availability="InStock")) == "in_stock"
    assert availability_of(page("", availability="https://schema.org/PreOrder")) == "preorder"


def test_the_words_are_read_when_there_is_no_structured_offer() -> None:
    assert availability_of(page("In Stock. Add to cart.")) == "in_stock"
    assert availability_of(page("Out of stock.")) == "out_of_stock"
    assert availability_of(page("Sold out")) == "out_of_stock"
    assert availability_of(page("Pre-order now")) == "preorder"


def test_a_switched_off_purchase_control_says_out_of_stock() -> None:
    assert availability_of(page("Add to cart disabled")) == "out_of_stock"
    assert availability_of(page("disabled Buy now")) == "out_of_stock"


def test_a_page_that_says_both_proves_neither() -> None:
    # One page describing several variants, or a template. It settles nothing
    # about this offer, and unknown is the honest answer.
    assert availability_of(page("512GB In Stock. 2TB Out of Stock.")) == "unknown"


def test_silence_is_unknown_and_stays_unknown() -> None:
    assert availability_of(page("A fast drive with a five year warranty.")) == "unknown"
    assert availability_of(page("")) == "unknown"


def test_unconfirmed_stock_cannot_be_the_cheapest_in_stock() -> None:
    limits = _evidence_limits(
        [
            {"product": "Drive A", "stock": "unknown"},
            {"product": "Drive B", "stock": "out_of_stock"},
        ]
    )
    assert limits["in_stock_confirmed"] == []
    assert limits["stock_not_confirmed"] == ["Drive A"]
    assert limits["not_available"] == ["Drive B"]
    assert "none of them can be called the cheapest" in limits["note"]


def test_a_confirmed_item_is_named_as_the_only_one_that_qualifies() -> None:
    limits = _evidence_limits(
        [
            {"product": "Drive A", "stock": "in_stock"},
            {"product": "Drive B", "stock": "unknown"},
        ]
    )
    assert limits["in_stock_confirmed"] == ["Drive A"]
    assert "Only the items listed under in_stock_confirmed" in limits["note"]
