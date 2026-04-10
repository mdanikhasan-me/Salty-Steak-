







const EXTERNAL = /^https?:\/\//i;

export function isExternalAddress(href) {
  return EXTERNAL.test(String(href || "").trim());
}





export function externalLinkFromEvent(event) {
  if (event.defaultPrevented) return "";
  if (event.button !== undefined && event.button !== 0) return "";

  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return "";
  const anchor = event.target?.closest?.("a[href]");
  if (!anchor) return "";
  const href = anchor.getAttribute("href") || "";
  return isExternalAddress(href) ? href : "";
}
