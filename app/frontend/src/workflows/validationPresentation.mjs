function count(value, fallback) {
  if (value == null || value === "") return fallback;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.max(number, fallback) : fallback;
}

export function validationPresentation(result = {}) {
  const blocking = Array.isArray(result.blocking_errors) ? result.blocking_errors
    : Array.isArray(result.errors) ? result.errors : [];
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  const blockingCount = count(result.blocking_error_count ?? result.blocking_count, blocking.length);
  const warningCount = count(result.warning_count, warnings.length);
  return { blocking, warnings, blockingCount, warningCount, clean: blockingCount === 0 && warningCount === 0 };
}
