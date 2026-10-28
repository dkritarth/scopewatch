import test from 'node:test';
import assert from 'node:assert/strict';
import { transformApiEvent, actionRequestStore, approvalStore, auditStore, clearStores, reconcileRunActionStates } from '../scripts/app.js';
import { filterEvents, STATUS_FACETS } from '../scripts/reviewer-state.js';

const cases = [
  ['WORKSPACE_PRUNE_ATTEMPTED', 'retention-attempted', 'attempted', /no result|unresolved/i],
  ['WORKSPACE_PRUNED', 'pruned', 'pruned', /completed|removed/i],
  ['WORKSPACE_PRUNE_FAILED', 'failed', 'failed', /partial/i],
];

function event(type, index) {
  return transformApiEvent({ id: `retention-${index}`, run_id: 'synthetic-run', sequence: index + 1,
    event_type: type, actor: 'synthetic-retention-operator', timestamp: '2026-10-28T04:00:00Z',
    summary: 'Synthetic operator workspace retention.',
    details: { attempt_id: `attempt-${index}`, workspace_key: 'synthetic-workspace', bytes: 165,
      outcome: type === 'WORKSPACE_PRUNED' ? 'pruned' : type === 'WORKSPACE_PRUNE_FAILED' ? 'failed' : undefined,
      reason: type === 'WORKSPACE_PRUNE_FAILED' ? 'workspace_removal_failed' : undefined } });
}

test('retention outcomes never invent action, policy, approval or execution evidence', () => {
  clearStores();
  for (const [index, [type, status, outcome, description]] of cases.entries()) {
    const rendered = event(type, index);
    reconcileRunActionStates([rendered]);
    assert.equal(rendered.status, status);
    assert.ok(STATUS_FACETS.includes(status));
    assert.match(rendered.statusLabel, /^Retention:/);
    assert.match(rendered.statusDescription, description);
    assert.equal(rendered.operatorRetention.outcome, outcome);
    assert.equal(rendered.operatorRetention.attemptId, `attempt-${index}`);
    assert.equal(rendered.action_request_id, null);
    assert.equal(rendered.policyOutcome, null);
    assert.equal(rendered.executionStatus, null);
    assert.equal(rendered.approvalStatus, null);
    assert.equal(rendered.reasoningAudit, null);
    assert.equal(rendered.tool, 'N/A');
    assert.equal(rendered.operation, 'N/A');
  }
  assert.equal(actionRequestStore.size + approvalStore.size + auditStore.size, 0);
});

test('retention failures use failed filter while attempts and success keep separate facets', () => {
  const events = cases.map(([type], index) => event(type, index));
  for (const status of ['failed', 'retention-attempted', 'pruned']) {
    assert.deepEqual(filterEvents(events, { status }).map(e => e.status), [status]);
  }
  for (const status of ['attempted', 'allowed', 'approved', 'executed']) {
    assert.deepEqual(filterEvents(events, { status }), []);
  }
});
