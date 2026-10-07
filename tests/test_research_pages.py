"""What a page is, and what may honestly be claimed from it.

The fixture below is the shape of the real failure: a retailer's catalogue
page announcing a span across every drive it sells, which was turned into the
quoted price of one specific 2TB drive and was wrong by roughly six times.
"""

from __future__ import annotations

from app.backend.research.pages import (
    ARTICLE_REVIEW,
    CATEGORY_LISTING,
    HOMEPAGE,
    PRODUCT_DETAIL,
    SEARCH_RESULTS,
    classify_page,
    looks_like_a_range,
    normalise_product,
    price_observation,
    same_product,
)

CATALOGUE = {
    "url": "https://shop.example.com/ssd",
    "title": "SSD Price in Bangladesh",
    "summary": (
        "SSD price starting from BDT 2,400 up to BDT 51,500, varieties of 382 "
        "products where 117 products are available. Sort by price low to high. "
        "Filter by brand. Showing 1 to 24. Next page. "
        "Lexar NM790 2TB 16,810 Tk. Samsung 990 Pro 2TB 24,500 Tk. "
        "Crucial P3 Plus 1TB 9,200 Tk. Kingston NV2 500GB 4,100 Tk. "
        "WD Black SN770 1TB 11,300 Tk. Adata Legend 800 1TB 7,900 Tk."
    ),
}

PRODUCT = {
    "url": "https://shop.example.com/lexar-nm790-2tb-gen-4-ssd",
    "title": "Lexar NM790 2TB Gen 4 NVMe M.2 2280 SSD",
    "summary": (
        "Lexar NM790 2TB PCIe Gen4 x4 NVMe M.2 2280 SSD. Price 16,810 Tk. "
        "Regular price 18,500 Tk. In stock. Add to cart. "
        "Warranty 5 years. Product code LNM790X002T. Specification: read up "
        "to 7400 MB/s."
    ),
}


def test_a_catalogue_is_not_mistaken_for_a_product_page() -> None:
    assert classify_page(CATALOGUE) == CATEGORY_LISTING


def test_a_product_page_is_recognised() -> None:
    assert classify_page(PRODUCT) == PRODUCT_DETAIL


def test_a_catalogue_never_yields_an_item_price() -> None:
    # This is the invariant the whole module exists for: the span
    # "from BDT 2,400 up to BDT 51,500" must never become one drive's price.
    assert price_observation(CATALOGUE, classify_page(CATALOGUE)) is None


def test_a_product_page_binds_product_price_seller_and_address() -> None:
    observation = price_observation(PRODUCT, classify_page(PRODUCT))

    assert observation is not None
    assert observation["product"].startswith("Lexar NM790 2TB")
    assert observation["price"] == 16810.0
    assert observation["currency"].casefold() == "tk"
    assert observation["seller"] == "shop.example.com"
    assert observation["url"] == PRODUCT["url"]
    assert observation["stock"] == "in_stock"
    assert observation["retrieved_at"] > 0


def test_a_range_statement_is_recognised_as_a_span() -> None:
    assert looks_like_a_range(
        "SSD price starting from BDT 2,400 up to BDT 51,500"
    ) is True
    assert looks_like_a_range("Price 16,810 Tk. In stock.") is False


def test_search_and_home_pages_are_typed() -> None:
    assert classify_page({"url": "https://shop.example.com/", "summary": ""}) == HOMEPAGE
    assert (
        classify_page({"url": "https://shop.example.com/search?q=ssd", "summary": "x"})
        == SEARCH_RESULTS
    )


def test_an_article_is_not_a_shop_page() -> None:
    article = {
        "url": "https://news.example.com/best-ssds-2026",
        "title": "The best SSDs of 2026",
        "summary": (
            "Published 3 March 2026. Written by a reviewer. "
            "We tested a dozen drives this year and the winner costs 16,000 Tk."
        ),
    }

    assert classify_page(article) == ARTICLE_REVIEW
    assert price_observation(article, classify_page(article)) is None


def test_a_capacity_variant_is_a_different_product() -> None:
    assert same_product(
        "Lexar NM790 2TB Gen 4 NVMe M.2 2280 SSD",
        "Lexar NM790 2TB PCIe Gen4 SSD",
    ) is True
    # The cheaper variant is not the item that was asked about.
    assert same_product(
        "Lexar NM790 2TB Gen 4 NVMe SSD",
        "Lexar NM790 1TB Gen 4 NVMe SSD",
    ) is False
    assert same_product(
        "Lexar NM790 2TB Gen 4 NVMe SSD",
        "Samsung 990 Pro 2TB NVMe SSD",
    ) is False


def test_product_identity_is_broken_into_the_parts_that_decide_it() -> None:
    parts = normalise_product("Lexar NM790 2TB PCIe Gen4 x4 NVMe M.2 2280 SSD")

    assert parts["model"] == "NM790"
    assert parts["capacity"] == "2TB"
    assert parts["generation"] == "gen4"
    assert parts["form_factor"] == "m.2"


def test_a_page_with_many_distinct_prices_yields_no_item_price() -> None:
    # A product page quotes its own price, perhaps beside a struck-through one.
    # A page quoting a dozen different figures binds none of them to one item,
    # so nothing is claimed from it however it is typed.
    crowded = {
        "url": "https://shop.example.com/deals",
        "title": "Deals",
        "summary": (
            "Add to cart. In stock. "
            + " ".join(f"Item {n} {n * 1000} Tk." for n in range(2, 12))
        ),
    }

    assert price_observation(crowded, classify_page(crowded)) is None


# --------------------------------------------------- the real live failure

# A researched turn visited this page, which is a genuine product page for a
# genuine 2TB drive, and reported that no price for it could be found. The
# page was typed "unknown" because a real retail product page carries an
# instalment table, a struck-through original and a delivery line — five
# distinct figures before anybody has quoted anything — and the classifier
# allowed four. Nothing could bind to an unknown page, so the whole
# conversation went on with no verified item in it.
LIVE_PRODUCT = {
    "url": "https://www.ultratech.com.bd/lexar-nm790-2tb-gen-4-ssd",
    "title": "Lexar NM790 2TB Gen 4 SSD price in BD",
    "summary": (
        "Lexar NM790 2TB Gen 4 SSD. Write a review 39,500৳ 48,500৳ "
        "EMI starts at 13,851৳/month. Save 9,000৳. Delivery charge 60৳. "
        "In stock. Add to cart. Warranty: 5 years. Product code: LNM790X002T. "
        "Specification: read up to 7400MB/s, write up to 6500MB/s. "
        "Related products: Samsung 990 Pro 2TB 24,500৳. "
        "Crucial P3 Plus 1TB 9,200৳."
    ),
}


def test_the_product_page_that_was_typed_unknown_is_a_product_page() -> None:
    assert classify_page(LIVE_PRODUCT) == PRODUCT_DETAIL


def test_an_instalment_is_not_what_the_product_costs() -> None:
    """The second half of the same failure.

    Had the page been typed correctly, the smallest figure on it would have
    been taken as the price — and the smallest figure is the 60 delivery
    charge, with the 13,851 monthly instalment close behind. Both are money
    printed beside a price and neither is one.
    """

    observation = price_observation(LIVE_PRODUCT, classify_page(LIVE_PRODUCT))
    assert observation is not None
    assert observation["price"] == 39500.0
    assert observation["currency"] == "৳"
    assert observation["stock"] == "in_stock"
    assert observation["url"] == LIVE_PRODUCT["url"]
    assert observation["seller"] == "www.ultratech.com.bd"


def test_a_price_of_zero_is_not_a_price() -> None:
    """Live failure, one turn after the last one was fixed.

    A drive whose page printed 0.00 was verified at ৳0.00 and became the
    cheapest thing the research had found — so "open the cheapest one you
    verified" pointed at the one product with no price at all.
    """

    call_for_price = {
        "url": "https://shop.example.com/crucial-e100-2tb-pcie-gen4-nvme-m2-ssd",
        "title": "Crucial E100 2TB PCIe Gen4 NVMe M.2 SSD Price in BD",
        "summary": (
            "Crucial E100 2TB PCIe Gen4 NVMe M.2 SSD. Price ৳0.00. "
            "Out of stock. Add to cart. Warranty: 5 years. "
            "Specification: PCIe Gen4 x4."
        ),
    }

    assert classify_page(call_for_price) == PRODUCT_DETAIL
    assert price_observation(call_for_price, PRODUCT_DETAIL) is None


def test_a_family_page_prices_no_single_variant() -> None:
    """The failure the user found by hand.

    A page selling 512GB, 1TB and 2TB from one address was read as
    "Hiksemi FUTURE 2TB - 13,200", because the title said 2TB and 13,200 was
    the cheapest figure on the page. It was the 512GB price. A title is not an
    offer, and a page with several offers prices none of them.
    """

    family = {
        "url": "https://shop.example.com/product/hiksemi-future-ssd",
        "title": "Hiksemi FUTURE 2TB M.2 NVMe PCIe Gen4x4 SSD",
        "summary": (
            "Hiksemi FUTURE SSD. 512GB ৳13,200. 1TB ৳22,500. 2TB ৳39,900. "
            "In stock. Add to cart. Warranty: 5 years. Product code: HS-FUTURE."
        ),
    }

    assert classify_page(family) == PRODUCT_DETAIL
    assert price_observation(family, PRODUCT_DETAIL) is None


def test_a_single_variant_page_still_prices_it() -> None:
    single = {
        "url": "https://shop.example.com/product/hiksemi-future-2tb",
        "title": "Hiksemi FUTURE 2TB M.2 NVMe PCIe Gen4x4 SSD",
        "summary": (
            "Hiksemi FUTURE 2TB M.2 NVMe PCIe Gen4x4 SSD. Price ৳39,900. "
            "In stock. Add to cart. Warranty: 5 years."
        ),
    }

    observation = price_observation(single, PRODUCT_DETAIL)
    assert observation is not None
    assert observation["price"] == 39900.0
    # The offer the price belongs to travels with it.
    assert observation["variant"] == "size:2097152"


def test_a_title_that_contradicts_the_body_claims_nothing() -> None:
    mismatched = {
        "url": "https://shop.example.com/product/x",
        "title": "Acme Drive 2TB NVMe SSD",
        "summary": "Acme Drive 512GB NVMe SSD. Price ৳9,900. In stock. Add to cart. Warranty.",
    }

    assert price_observation(mismatched, PRODUCT_DETAIL) is None


def test_the_same_size_written_two_ways_is_one_offer() -> None:
    from app.backend.research.pages import variant_values

    assert variant_values("2TB drive") == variant_values("2048GB drive")
    assert len(variant_values("available in 512GB and 2TB")) == 2


def test_a_recommendation_further_down_the_page_is_not_this_product() -> None:
    # "Related products: ... 9,200" is a different drive. Reading past that
    # boundary is how the cheapest thing on the page becomes the answer.
    from app.backend.research.pages import own_product_text

    kept = own_product_text(LIVE_PRODUCT["summary"])
    assert "39,500" in kept
    assert "9,200" not in kept
    assert "Samsung" not in kept
