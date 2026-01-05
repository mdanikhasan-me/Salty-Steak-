import { formatNumber } from "../../workflows/formatters.js";
import { Disclosure, InlineNotice } from "../../components/Primitives.jsx";

export function ValidationReport({ result }) {
  const blocking = result.blocking_errors || result.errors || [];
  const warnings = result.warnings || [];

  return (
    <div className="validation-report">
      <h3>Validation result</h3>
      <div className="validation-totals">
        <span>
          <strong>{formatNumber(result.records_checked ?? result.record_count)}</strong>
          records checked
        </span>
        <span className={blocking.length ? "text-error" : ""}>
          <strong>
            {formatNumber(
              result.blocking_error_count ?? result.blocking_count ?? blocking.length,
            )}
          </strong>
          blocking errors
        </span>
        <span className={warnings.length ? "text-warning" : ""}>
          <strong>{formatNumber(result.warning_count ?? warnings.length)}</strong>
          warnings
        </span>
      </div>
      {!blocking.length && !warnings.length ? (
        <InlineNotice kind="success" title="No validation issues found">
          This dataset can be prepared for training.
        </InlineNotice>
      ) : (
        <>
          {blocking.length ? (
            <IssueList title="Blocking errors" issues={blocking} kind="error" />
          ) : null}
          {warnings.length ? (
            <IssueList title="Warnings" issues={warnings} kind="warning" />
          ) : null}
        </>
      )}
    </div>
  );
}

function IssueList({ title, issues, kind }) {
  return (
    <section className={`issue-list issue-list--${kind}`}>
      <h4>{title}</h4>
      {issues.map((issue, index) => (
        <div key={issue.code || `${title}-${index}`}>
          <div className="issue-list__heading">
            <strong>{issue.message || issue.title || String(issue)}</strong>
            {issue.affected_count !== undefined ? (
              <span>{formatNumber(issue.affected_count)} affected</span>
            ) : null}
          </div>
          {issue.recommended_action ? <p>{issue.recommended_action}</p> : null}
          {issue.examples?.length ? (
            <Disclosure
              summary={`${issue.examples.length} real example${issue.examples.length === 1 ? "" : "s"}`}
            >
              <pre>{issue.examples.map((example) => JSON.stringify(example, null, 2)).join("\n\n")}</pre>
            </Disclosure>
          ) : null}
        </div>
      ))}
    </section>
  );
}
