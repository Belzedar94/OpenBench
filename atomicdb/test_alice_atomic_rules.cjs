'use strict';

// The browser's Alice Atomic rules against the vectors the server's rules
// wrote (atomicdb/funquests/alice_atomic_vectors.json).  Run with
// `node atomicdb/test_alice_atomic_rules.cjs`; the Python suite runs it too
// when Node is installed (test_funquests_rules.py).

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const rules = require(path.join(
  __dirname, 'static', 'atomicdb', 'funquests', 'alice_atomic.js',
));
const vectors = JSON.parse(fs.readFileSync(path.join(
  __dirname, 'funquests', 'alice_atomic_vectors.json',
), 'utf8'));

let checked = 0;
for (const v of vectors.positions) {
  const state = rules.parse(v.afen);
  assert.equal(rules.toAfen(state), v.afen, 'round trip');
  assert.deepEqual(rules.legalMoves(state).sort(), v.legal, v.afen);
  assert.equal(rules.inCheck(state, state.turn), v.in_check, v.afen);
  assert.deepEqual(rules.outcome(state, 1), v.outcome, v.afen);
  // Every move with the position it leads to, digested as the server does.
  const lines = rules.legalMovesDetailed(state)
    .map((x) => `${x.text} ${rules.toAfen(x.after)}`)
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const digest = crypto.createHash('sha1').update(lines.join('\n'))
    .digest('hex');
  assert.equal(digest, v.afters, `positions after the moves of ${v.afen}`);
  checked += 1;
}
for (const p of vectors.perft) {
  assert.equal(rules.perft(rules.parse(p.afen), p.depth), p.nodes,
    `perft ${p.depth}`);
}
// No en passant: an old position with a square in the field reads as if
// it were "-", and the field is always written "-".
{
  const old = rules.parse('4k3/8/8/8/8/8/8/4K3|8/8/8/8/3pP3/8/8/8 b - Be3 0 1');
  assert.equal(rules.toAfen(old),
    '4k3/8/8/8/8/8/8/4K3|8/8/8/8/3pP3/8/8/8 b - - 0 1');
  assert.ok(!rules.legalMoves(old).includes('Bd4e3'));
  assert.throws(() => rules.parse(
    '4k3/8/8/8/8/8/8/4K3|8/8/8/8/8/8/8/8 w - Ce3 0 1'));
  assert.equal(rules.EN_PASSANT, undefined);
}
assert.ok(checked > 50);
console.log(`ok ${checked} positions, ${vectors.perft.length} perft counts`);
