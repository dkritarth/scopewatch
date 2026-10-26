import test from 'node:test';
import assert from 'node:assert/strict';
import { reviewCounts } from '../scripts/app.js';
test('home counters count unique actual executions and policy denials, never allowed or attempted actions', () => {
  assert.deepEqual(reviewCounts([
    { id: 'a', eventType: 'POLICY_ALLOWED', action_request_id: 'one' },
    { id: 'b', eventType: 'EXECUTION_STARTED', action_request_id: 'one' },
    { id: 'c', eventType: 'EXECUTION_SUCCEEDED', action_request_id: 'one' },
    { id: 'c-copy', eventType: 'EXECUTION_SUCCEEDED', action_request_id: 'one' },
    { id: 'd', eventType: 'POLICY_DENIED', action_request_id: 'two' },
    { id: 'e', eventType: 'APPROVAL_DENIED', action_request_id: 'three' },
  ]), { executed: 1, denied: 1 });
  assert.deepEqual(reviewCounts(), { executed: 0, denied: 0 });
});
