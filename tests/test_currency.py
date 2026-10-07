"""A currency is a fact. A symbol is typography.

The installed application answered a Bangladesh price question with "₹43,500".
The number was right and the answer was untrustworthy, because that mark means
Indian rupees. Replacing one character with another does not fix it — the
mistake was carrying presentation where evidence belonged.
"""

from __future__ import annotations

import pytest

from app.backend.research.pages import (
    PRODUCT_DETAIL,
    currency_code,
    format_price,
    price_observation,
)


@pytest.mark.parametrize(
    "written, expected",
    [
        ("৳", "BDT"),
        ("Tk", "BDT"),
        ("tk", "BDT"),
        ("BDT", "BDT"),
        ("₹", "INR"),
        ("Rs", "INR"),
        ("INR", "INR"),
        ("$", "USD"),
        ("USD", "USD"),
        ("€", "EUR"),
        ("£", "GBP"),
    ],
)
def test_every_way_a_shop_writes_money_resolves_to_one_code(written, expected) -> None:
    assert currency_code(written) == expected


def test_an_unrecognised_mark_is_not_guessed() -> None:
    # Deciding that one currency is another turns a right number into a wrong
    # price, so uncertainty is preserved instead.
    assert currency_code("¤") == ""
    assert currency_code("") == ""
    assert currency_code("credits") == ""


def test_presentation_lives_in_one_place() -> None:
    assert format_price(43500, "BDT") == "৳43,500"
    assert format_price(43500, "INR") == "₹43,500"
    assert format_price(100, "USD") == "$100"
    # A code with no house style still reads as money rather than as a bare
    # number.
    assert format_price(1200, "JPY") == "1,200 JPY"


@pytest.mark.parametrize(
    "written",
    ["Price ৳43,500.", "Price Tk 43,500.", "Price 43,500 Tk.", "Price BDT 43,500."],
)
def test_a_bangladesh_page_is_never_read_as_rupees(written) -> None:
    page = {
        "url": "https://shop.example.com/lexar-nm790-2tb",
        "title": "Lexar NM790 2TB Gen4 NVMe SSD",
        "summary": f"Lexar NM790 2TB Gen4 NVMe SSD. {written} In stock. Add to cart. Warranty.",
    }

    observation = price_observation(page, PRODUCT_DETAIL)
    assert observation is not None
    assert observation["currency_code"] == "BDT"
    assert observation["price_display"] == "৳43,500"
    assert "₹" not in observation["price_display"]


def test_a_rupee_page_stays_rupees() -> None:
    page = {
        "url": "https://shop.example.in/drive",
        "title": "Acme 2TB NVMe SSD",
        "summary": "Acme 2TB NVMe SSD. Price ₹5,000. In stock. Add to cart. Warranty.",
    }

    observation = price_observation(page, PRODUCT_DETAIL)
    assert observation["currency_code"] == "INR"
    assert observation["price_display"] == "₹5,000"
