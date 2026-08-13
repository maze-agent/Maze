import assert from 'node:assert/strict';
import test from 'node:test';
import { errorMessage } from './errors.ts';

test('errorMessage renders structured errors instead of object coercion', () => {
  assert.equal(errorMessage({ error_type: 'user_code', message: 'image.png was not found' }), 'image.png was not found');
  assert.equal(errorMessage({ error: { message: 'nested failure' } }), 'nested failure');
  assert.equal(errorMessage({ code: 'UNKNOWN' }), '{"code":"UNKNOWN"}');
});
