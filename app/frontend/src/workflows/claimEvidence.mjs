














const CLAIM_LABELS = {
  price: "Price",
  variant: "Capacity",
  availability: "Stock",
  seller: "Seller",
};

function host(url) {
  try {
    return new URL(String(url)).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function siteIcon(url) {
  try {
    const parsed = new URL(String(url));
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "";
    return `${parsed.protocol}//${parsed.host}/favicon.ico`;
  } catch {
    return "";
  }
}

const STOCK_WORDS = {
  in_stock: "In stock",
  out_of_stock: "Out of stock",
  preorder: "Pre-order",
  unknown: "Not confirmed",
};


export function variantLabel(variant) {
  const match = /^size:(\d+)$/.exec(String(variant || ""));
  if (!match) return String(variant || "");
  const megabytes = Number(match[1]);
  if (megabytes >= 1024 * 1024) return `${megabytes / (1024 * 1024)}TB`;
  if (megabytes >= 1024) return `${megabytes / 1024}GB`;
  return `${megabytes}MB`;
}








export function claimsFrom(details) {



  const observations = (details?.orchestration?.observations || [])
    .map((item, index) => ({ item, ref: `e${index + 1}` }))
    .filter(
      ({ item }) =>
        item && item.url && String(item.status || "active") === "active",
    );

  const claims = [];
  observations.forEach(({ item, ref }) => {
    const source = {
      ref,
      host: host(item.url),
      icon: siteIcon(item.url),
      url: String(item.url),
      title: String(item.product || ""),
      retrievedAt: Number(item.retrieved_at) || null,
    };
    const add = (type, value) => {
      if (!value) return;
      claims.push({
        id: `${source.ref}-${type}`,
        type,
        label: CLAIM_LABELS[type] || type,
        value: String(value),
        source,
      });
    };
    add("price", item.price_display || item.price);
    add("variant", variantLabel(item.variant));


    if (String(item.stock || "unknown") !== "unknown") {
      add("availability", STOCK_WORDS[String(item.stock)] || String(item.stock));
    }
    add("seller", source.host);
  });
  return claims;
}




export function evidenceFor(details, ref) {
  return claimsFrom(details).filter((claim) => claim.source.ref === ref);
}


export function usedSources(details) {
  const bySource = new Map();
  for (const claim of claimsFrom(details)) {
    const existing = bySource.get(claim.source.ref);
    if (existing) {
      existing.supports.push(claim.label);
      continue;
    }
    bySource.set(claim.source.ref, {
      ...claim.source,
      supports: [claim.label],


      snippet: "",
    });
  }
  const rows = [...bySource.values()];
  for (const row of rows) {
    const claims = evidenceFor(details, row.ref);
    row.snippet = claims
      .filter((claim) => claim.type !== "seller")
      .map((claim) => claim.value)
      .join(" · ");
  }
  return rows;
}

export default claimsFrom;
