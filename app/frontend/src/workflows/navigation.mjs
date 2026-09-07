export const DEFAULT_TRAINING_PAGE = "data";
export const TRAINING_PAGES = Object.freeze(["data", "train", "versions", "evaluate", "system"]);
export const APP_PAGES = new Set(["chat", "about", "evaluate", ...TRAINING_PAGES, "project"]);

export function pageFromHash(hash) {
  const token = String(hash || "")
    .replace(/^#/, "")
    .split("?")[0];
  return APP_PAGES.has(token) ? token : "chat";
}

export function isTrainingPage(page) {
  return TRAINING_PAGES.includes(page);
}

export function validTrainingPage(page) {
  return isTrainingPage(page) ? page : DEFAULT_TRAINING_PAGE;
}

export function hashForPage(page, parameters = {}) {
  const safePage = APP_PAGES.has(page) ? page : "chat";
  const query = new URLSearchParams(parameters).toString();
  return `#${safePage}${query ? `?${query}` : ""}`;
}
