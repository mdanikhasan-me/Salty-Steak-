"""What kind of page this is, and what may honestly be claimed from it.

A retailer's category page and a retailer's product page look the same to a
sentence splitter, and that is how "382 products from 2,400 to 51,500" — a
range across every drive on a site, including small ones — became the quoted
price of a specific 2TB drive. The answer was wrong by roughly six times and
no amount of instructing the model not to do it fixes evidence that arrived
already broken.

So the page is typed before anything is extracted from it, and a price may
only be attached to a product when one page binds them together. Everything
here works from generic structure and vocabulary — counts, ranges, cart
controls, pagination — never from a retailer's name, so it holds for a shop
nobody here has heard of.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

PRODUCT_DETAIL = "product_detail"
CATEGORY_LISTING = "category_listing"
SEARCH_RESULTS = "search_results"
ARTICLE_REVIEW = "article_review"
HOMEPAGE = "homepage"
UNKNOWN = "unknown"



_AMOUNT = r"\d{1,3}(?:[, \s]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_CURRENCY = r"[৳₹$£€¥]|(?:BDT|USD|EUR|GBP|INR|TK|Tk|tk)"
PRICE = re.compile(
    rf"(?:(?P<pre>{_CURRENCY})\s?(?P<a>{_AMOUNT}))|(?:(?P<b>{_AMOUNT})\s?(?P<post>{_CURRENCY}))"
)


RANGE = re.compile(
    r"(?:from|between|starting (?:from|at)|ranges? from)\b[^.]{0,80}?"
    r"(?:to|-|–|—|and)\b",
    re.IGNORECASE,
)

_LISTING_PHRASES = (
    "products",
    "items found",
    "results found",
    "showing",
    "sort by",
    "filter by",
    "price low to high",
    "price high to low",
    "per page",
    "next page",
    "load more",
    "showing 1",
)

_PRODUCT_PHRASES = (
    "add to cart",
    "add to basket",
    "buy now",
    "in stock",
    "out of stock",
    "stock status",
    "availability",
    "warranty",
    "part number",
    "product code",
    "sku",
    "model:",
    "specification",
)

_ARTICLE_PHRASES = (
    "published",
    "last updated",
    "written by",
    "reading time",
    "comments",
)




_RELATED_BOUNDARIES = (
    "related product",
    "similar product",
    "you may also like",
    "you might also like",
    "customers also",
    "frequently bought",
    "recommended for you",
    "recently viewed",
    "compare with similar",
    "more from this",
)











_LABEL_BEFORE = (
    "emi",
    "instal",
    "save",
    "discount",
    "cashback",
    "delivery",
    "shipping",
    "coupon",
    "voucher",
    "reward",
    "% off",
)

_PERIOD_AFTER = ("/month", "/mo", "per month", "a month", "monthly", "/year")



_LABEL_WINDOW = 24
_PERIOD_WINDOW = 10


def _counts(text: str) -> tuple[int, int]:
    """How many priced amounts the page carries, and how many are distinct."""

    values = []
    for match in PRICE.finditer(text):
        raw = match.group("a") or match.group("b") or ""
        cleaned = raw.replace(",", "").replace(" ", "").replace(" ", "")
        try:
            values.append(float(cleaned))
        except ValueError:
            continue
    return len(values), len(set(values))


def _hits(text: str, phrases: tuple[str, ...]) -> int:
    return sum(1 for phrase in phrases if phrase in text)












VARIANT_TOKENS = re.compile(
    r"\b(\d+(?:\.\d+)?)\s?(tb|gb|mb|inch|\"|pack|pcs|pieces)\b"
    r"(?!\s*(?:/\s*s(?:ec)?\b|ps\b|/\s*second\b))",
    re.IGNORECASE,
)


_UNIT_SCALE = {"mb": 1, "gb": 1024, "tb": 1024 * 1024}


def variant_values(text: str) -> set[str]:
    """The distinct variant values a piece of text offers.

    Storage sizes are normalised to one unit so a page quoting "2TB" and
    "2048GB" is understood to be describing one offer rather than two.
    """

    found: set[str] = set()
    for match in VARIANT_TOKENS.finditer(str(text or "")):
        try:
            amount = float(match.group(1))
        except ValueError:
            continue
        unit = match.group(2).casefold()
        scale = _UNIT_SCALE.get(unit)
        if scale is not None:
            found.add(f"size:{int(amount * scale)}")
        else:
            found.add(f"{unit}:{amount:g}")
    return found






IN_STOCK = re.compile(
    r"\b(?:in\s?stock|available\s+now|ready\s+to\s+ship|"
    r"schema\.org/InStock|\"InStock\"|>InStock<)\b",
    re.IGNORECASE,
)
OUT_OF_STOCK = re.compile(
    r"\b(?:out\s?of\s?stock|sold\s?out|unavailable|discontinued|"
    r"schema\.org/OutOfStock|\"OutOfStock\"|>OutOfStock<|currently\s+unavailable)\b",
    re.IGNORECASE,
)
PREORDER = re.compile(
    r"\b(?:pre-?order|back-?order|schema\.org/(?:PreOrder|BackOrder))\b",
    re.IGNORECASE,
)

DISABLED_PURCHASE = re.compile(
    r"(?:add\s+to\s+(?:cart|basket)|buy\s+now)[^<>]{0,60}"
    r"(?:disabled|unavailable|out\s+of\s+stock)"
    r"|disabled[^<>]{0,40}(?:add\s+to\s+(?:cart|basket)|buy\s+now)",
    re.IGNORECASE,
)

UNKNOWN_STOCK = "unknown"






CURRENCY_CODES = {
    "৳": "BDT",
    "tk": "BDT",
    "tk.": "BDT",
    "bdt": "BDT",
    "৳.": "BDT",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "us$": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}



CURRENCY_DISPLAY = {
    "BDT": "৳",
    "INR": "₹",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}


def currency_code(written: str) -> str:
    """The currency a shop meant, or nothing when it cannot be told.

    Never guessed from an ambiguous mark: silently deciding that one currency
    is another turns a right number into a wrong price.
    """

    text = " ".join(str(written or "").split()).casefold()
    return CURRENCY_CODES.get(text, "")


def format_price(amount: float, code: str) -> str:
    """One place decides how money is written."""

    try:
        figure = f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return ""
    symbol = CURRENCY_DISPLAY.get(str(code or "").upper())
    if symbol:
        return f"{symbol}{figure}"
    return f"{figure} {code}".strip() if code else figure


def availability_of(page: Mapping[str, Any], text: str = "") -> str:
    """Whether this offer can actually be bought, from the strongest evidence.

    Availability is its own observation, never an assumption. "Unknown" is a
    real answer and must stay one: silently reading it as in stock is how
    "cheapest currently in stock" ends up naming something nobody can buy.

    Structured data wins over prose, because a shop's own machine-readable
    offer is what its checkout uses. Contradictions resolve to unknown rather
    than to whichever reading is convenient.
    """

    declared = str(page.get("availability") or "").strip()
    if declared:
        lowered = declared.casefold()
        if "outofstock" in lowered.replace(" ", "") or "soldout" in lowered.replace(" ", ""):
            return "out_of_stock"
        if "preorder" in lowered.replace(" ", "") or "backorder" in lowered.replace(" ", ""):
            return "preorder"
        if "instock" in lowered.replace(" ", ""):
            return "in_stock"

    body = str(text or page.get("summary") or page.get("text") or "")
    if DISABLED_PURCHASE.search(body):
        return "out_of_stock"

    out = bool(OUT_OF_STOCK.search(body))
    pre = bool(PREORDER.search(body))
    inn = bool(IN_STOCK.search(body))
    if out and inn:


        return UNKNOWN_STOCK
    if out:
        return "out_of_stock"
    if pre:
        return "preorder"
    if inn:
        return "in_stock"
    return UNKNOWN_STOCK


def own_product_text(text: str) -> str:
    """The part of a page that is about the product the page is for."""

    lowered = str(text or "").casefold()
    cut = len(text or "")
    for boundary in _RELATED_BOUNDARIES:
        found = lowered.find(boundary)
        if found >= 0:
            cut = min(cut, found)
    return str(text or "")[:cut]


def _is_the_products_own_price(text: str, match: re.Match[str]) -> bool:
    """Whether an amount on a product page is what the product costs.

    An instalment, a saving and a delivery charge are all money printed beside
    a price, and none of them is one.
    """

    before = text[max(0, match.start() - _LABEL_WINDOW) : match.start()].casefold()
    after = text[match.end() : match.end() + _PERIOD_WINDOW].casefold()
    if any(label in before for label in _LABEL_BEFORE):
        return False
    return not any(period in after for period in _PERIOD_AFTER)


def classify_page(page: Mapping[str, Any]) -> str:
    """Decide what kind of page this is from its own structure.

    Deliberately conservative in one direction: a page that shows many prices
    and a product count is a listing even if it also has a cart control,
    because mistaking a listing for a product page is the failure that puts a
    wrong number in front of the user.
    """

    text = f"{page.get('title') or ''}\n{page.get('summary') or page.get('text') or ''}"
    lowered = text.casefold()
    url = str(page.get("url") or "")
    path = urlparse(url).path.strip("/") if url else ""
    query = urlparse(url).query if url else ""

    total_prices, distinct_prices = _counts(text)
    listing = _hits(lowered, _LISTING_PHRASES)
    product = _hits(lowered, _PRODUCT_PHRASES)
    article = _hits(lowered, _ARTICLE_PHRASES)
    counted = re.search(r"\b\d{2,}\s+(?:products|items|results)\b", lowered)

    if not path and not query:
        return HOMEPAGE
    if "search" in path.casefold() or re.search(r"(^|&)(q|s|search|keyword)=", query):
        return SEARCH_RESULTS


    if counted or (distinct_prices >= 6 and listing >= 2):
        return CATEGORY_LISTING






    if product >= 2 and (listing <= 1 or distinct_prices <= 4):
        return PRODUCT_DETAIL
    if article >= 2 and distinct_prices <= 2:
        return ARTICLE_REVIEW
    if listing >= 3:
        return CATEGORY_LISTING
    return UNKNOWN


def looks_like_a_range(statement: str) -> bool:
    """Whether a statement gives a span of prices rather than one price."""

    if not RANGE.search(statement):
        return False
    total, _ = _counts(statement)
    return total >= 2


def _amount(match: re.Match[str]) -> tuple[str, float] | None:
    raw = match.group("a") or match.group("b") or ""
    currency = (match.group("pre") or match.group("post") or "").strip()
    cleaned = raw.replace(",", "").replace(" ", "").replace(" ", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None




    if value <= 0:
        return None
    return currency, value


def price_observation(page: Mapping[str, Any], page_type: str) -> dict[str, Any] | None:
    """One page's price for one product, or nothing.

    Returned only when a single page binds a product identity, a price, a
    currency, a seller and its own address together. A listing binds none of
    those to each other, so it never produces one — which is the whole point.
    """

    if page_type != PRODUCT_DETAIL:
        return None
    title = " ".join(str(page.get("title") or "").split())
    url = str(page.get("url") or "")
    if not title or not url:
        return None




    text = own_product_text(str(page.get("summary") or page.get("text") or ""))
    found = [
        value
        for value in (
            _amount(match)
            for match in PRICE.finditer(text)
            if _is_the_products_own_price(text, match)
        )
        if value
    ]
    if not found:
        return None



    distinct = {value for _, value in found}
    if len(distinct) > 4:
        return None








    offered = variant_values(text)
    wanted = variant_values(title)
    if len(offered) > 1:


        return None
    if wanted and offered and not (wanted & offered):


        return None



    currency, price = min(found, key=lambda item: item[1])
    if not currency:
        currency = next((unit for unit, _ in found if unit), "")
    code = currency_code(currency)
    if not code:



        for unit, _ in found:
            code = currency_code(unit)
            if code:
                break

    stock = availability_of(page, text)
    return {
        "seller": urlparse(url).hostname or "",
        "product": title,
        "price": price,
        "currency": currency or "",



        "currency_code": code,
        "price_display": format_price(price, code) if code else "",
        "url": url,
        "stock": stock,
        "page_type": page_type,


        "variant": sorted(wanted or offered)[0] if (wanted or offered) else "",
        "retrieved_at": time.time(),
    }


def normalise_product(title: str) -> dict[str, str]:
    """The parts of a product name that decide whether two pages agree.

    A 1TB drive and a 2TB drive of the same model are different products, and
    treating them as one is how a cheaper variant's price ends up quoted for
    the more expensive one.
    """

    text = " ".join(str(title or "").split())
    lowered = text.casefold()
    capacity = ""
    match = re.search(r"\b(\d+(?:\.\d+)?)\s?(tb|gb)\b", lowered)
    if match:
        capacity = f"{match.group(1)}{match.group(2).upper()}"
    generation = ""
    gen = re.search(r"\bgen\s?(\d)\b|\bpcie\s?(\d)\.0\b", lowered)
    if gen:
        generation = f"gen{gen.group(1) or gen.group(2)}"
    form = ""
    if "m.2" in lowered or "m2 2280" in lowered:
        form = "m.2"
    elif "2.5" in lowered:
        form = "2.5in"
    model = ""
    tokens = re.findall(r"\b[a-z]{1,4}\d{2,4}[a-z]?\b", lowered)
    if tokens:
        model = tokens[0].upper()
    return {
        "title": text,
        "model": model,
        "capacity": capacity,
        "generation": generation,
        "form_factor": form,
    }


def same_product(left: str, right: str) -> bool:
    """Whether two titles name the same item, not merely the same family."""

    a, b = normalise_product(left), normalise_product(right)
    if a["model"] and b["model"] and a["model"] != b["model"]:
        return False
    if a["capacity"] and b["capacity"] and a["capacity"] != b["capacity"]:
        return False
    return bool(a["model"] and a["model"] == b["model"] and a["capacity"] == b["capacity"])
