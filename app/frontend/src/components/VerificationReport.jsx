import { executionEvidenceLabel } from '../workflows/verificationEvidence.mjs';

export function VerificationReport({ report }) {
  if (!report) return null;
  const attempts = Array.isArray(report.attempts) ? report.attempts : [report];
  const label = report.status === "passed" ? "Recorded checks passed"
    : report.status === "failed" ? "Some checks still failed"
    : report.status === "testing" ? "Running local checks"
    : report.status === "repairing" ? "Repairing the draft" : "Not fully verified";
  return <section className="response-verification" aria-label="Lock In checks">
    <h3>Lock In checks</h3><p>{label}</p>
    {report.scope ? <small>{report.scope}</small> : null}
    {report.name ? <small>{report.name}</small> : null}
    {report.repairs > 0 ? <small>{report.repairs} repair attempts</small> : null}
    {Array.isArray(report.requirements) ? <details><summary>Requirements checked</summary><ul>
      {report.requirements.map((item,index)=><li key={index}>{item}{report.covered_requirements?.includes(index) ? " — checked" : " — unverified"}</li>)}
    </ul></details> : null}
    {attempts.map((attempt,index)=>Array.isArray(attempt.checks)&&attempt.checks.length ? <details key={index} className="verification-attempt">
      <summary>Attempt {index+1} · {attempt.status === "passed" ? "passed" : attempt.status === "failed" ? "failed" : "unverified"}</summary>
      {attempt.checks.map((check,i)=><div className="verification-command" key={i}>
        <strong>{check.name}</strong><small>{check.kind} · {check.status} · exit {check.exit_code ?? "—"}</small>
        {executionEvidenceLabel(check) ? <small>{executionEvidenceLabel(check)}</small> : null}
        {Array.isArray(check.argv) ? <code>{check.argv.join(" ")}</code> : null}
        {check.stdout ? <pre aria-label="Standard output">{check.stdout}</pre> : null}
        {check.stderr ? <pre aria-label="Standard error">{check.stderr}</pre> : null}
        {check.error ? <p>{check.error}</p> : null}
        {check.output_truncated ? <small>Output excerpt truncated.</small> : null}
      </div>)}
    </details> : null)}
    {Array.isArray(report.issues)&&report.issues.length ? <ul>{report.issues.map((issue,index)=><li key={index}>{issue}</li>)}</ul> : null}
    {Array.isArray(report.limitations)&&report.limitations.length ? <details><summary>Limits of these checks</summary><ul>{report.limitations.map((item,index)=><li key={index}>{item}</li>)}</ul></details> : null}
    {report.work_directory ? <details><summary>Saved verification files</summary><code>{report.work_directory}</code></details> : null}
  </section>;
}
