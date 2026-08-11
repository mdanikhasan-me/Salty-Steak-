export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "Not reported";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const order = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / 1024 ** order;
  return `${amount >= 10 || order === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[order]}`;
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "Not reported";
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat().format(number) : "Not reported";
}

export function formatDate(value) {
  if (typeof value !== "string" || !value.trim()) return "Unavailable";


  if (!/^\d{4}-\d{2}-\d{2}T/.test(value)) return "Unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "Not reported";
  if (total > 0 && total < 10) {
    return `${total.toFixed(1).replace(/\.0$/, "")} sec`;
  }
  if (total < 60) return `${Math.round(total)} sec`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return `${hours} hr ${minutes} min`;
  return `${minutes} min`;
}

export function formatRate(value, suffix) {
  const number = Number(value);
  return Number.isFinite(number)
    ? `${number.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${suffix}`
    : "Not reported";
}

export function versionLabel(version) {
  if (!version) return "Unknown saved version";
  if (version.display_label) return version.display_label;
  if (version.label) return version.label;
  const steps =
    version.total_trained_steps ??
    version.total_steps ??
    version.totalSteps;
  if (steps !== "" && steps !== null && steps !== undefined && Number.isFinite(Number(steps))) {
    return `Salty Steak at ${formatNumber(steps)} total steps`;
  }
  const importedNumber = version.imported_number ?? version.importedNumber;
  return importedNumber
    ? `Salty Steak imported version ${importedNumber}`
    : "Salty Steak imported checkpoint";
}

export function errorMessage(error, fallback = "Something went wrong.") {
  return error?.message || error?.detail || String(error || fallback);
}

export function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}
