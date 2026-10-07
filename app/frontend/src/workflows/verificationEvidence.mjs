export function executionEvidenceLabel(check) {
  const evidence = check?.test_evidence;
  if (!evidence) return '';
  if (Number.isInteger(evidence.passing)) {
    const count = evidence.passing;
    return `${count} passing test${count === 1 ? '' : 's'} observed${
      evidence.behavior_verified === false ? ' — behavior unverified' : ''}`;
  }
  if (evidence.behavior_verified === false) return 'No passing behavior observed';
  if (evidence.runner === 'custom') return 'Custom assertions; test count not reported';
  return '';
}
