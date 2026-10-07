import test from 'node:test';
import assert from 'node:assert/strict';
import { executionEvidenceLabel } from './verificationEvidence.mjs';

test('exit zero does not become a passing test count', () => {
  assert.equal(executionEvidenceLabel({exit_code:0}), '');
  assert.equal(executionEvidenceLabel({test_evidence:{passing:0,behavior_verified:false}}),
    '0 passing tests observed — behavior unverified');
});
test('observed counts remain explicit and custom checks do not invent them', () => {
  assert.equal(executionEvidenceLabel({test_evidence:{passing:1,behavior_verified:true}}),
    '1 passing test observed');
  assert.equal(executionEvidenceLabel({test_evidence:{runner:'custom',behavior_verified:true}}),
    'Custom assertions; test count not reported');
});
