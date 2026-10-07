import assert from "node:assert/strict";
import test from "node:test";

import { claimsFrom, evidenceFor, usedSources, variantLabel } from "./claimEvidence.mjs";

const DETAILS = {
  orchestration: {
    kind: "research",
    observations: [
      {
        product: "Lexar NM790 2TB Gen4 NVMe SSD",
        price: 40999,
        price_display: "৳40,999",
        currency_code: "BDT",
        variant: "size:2097152",
        seller: "www.ryans.com",
        stock: "in_stock",
        url: "https://www.ryans.com/lexar-nm790-2tb",
      },
      {
        product: "Samsung 990 Pro 2TB",
        price: 51000,
        price_display: "৳51,000",
        variant: "size:2097152",
        stock: "unknown",
        url: "https://www.startech.com.bd/samsung-990-pro-2tb",
      },
    ],
  },
};

test("each claim points at the one page that proves it", () => {
  const claims = claimsFrom(DETAILS);
  const price = claims.find((claim) => claim.type === "price");
  assert.equal(price.value, "৳40,999");
  assert.equal(price.source.host, "ryans.com");
  assert.equal(price.source.url, "https://www.ryans.com/lexar-nm790-2tb");
});

test("a claim never borrows another page's evidence", () => {

  const ryans = evidenceFor(DETAILS, "e1").map((claim) => claim.type);
  assert.deepEqual(ryans, ["price", "variant", "availability", "seller"]);
  for (const claim of evidenceFor(DETAILS, "e2")) {
    assert.equal(claim.source.host, "startech.com.bd");
  }
});

test("no page proves an absence, so unconfirmed stock gets no chip", () => {
  const startech = evidenceFor(DETAILS, "e2").map((claim) => claim.type);
  assert.ok(!startech.includes("availability"));
  assert.ok(startech.includes("price"));
});

test("capacity is readable rather than a normalised key", () => {
  assert.equal(variantLabel("size:2097152"), "2TB");
  assert.equal(variantLabel("size:524288"), "512GB");
  assert.equal(variantLabel(""), "");
});

test("disproven evidence stops proving things", () => {
  const corrected = {
    orchestration: {
      observations: [
        { ...DETAILS.orchestration.observations[0], status: "superseded" },
        DETAILS.orchestration.observations[1],
      ],
    },
  };
  assert.deepEqual(evidenceFor(corrected, "e1"), []);
  assert.ok(claimsFrom(corrected).length > 0);
});

test("a source row says why it mattered", () => {
  const [ryans] = usedSources(DETAILS);
  assert.equal(ryans.host, "ryans.com");
  assert.equal(ryans.snippet, "৳40,999 · 2TB · In stock");
  assert.deepEqual(ryans.supports, ["Price", "Capacity", "Stock", "Seller"]);
});

test("a turn with no observations has no claims", () => {
  assert.deepEqual(claimsFrom({}), []);
  assert.deepEqual(usedSources({}), []);
});
