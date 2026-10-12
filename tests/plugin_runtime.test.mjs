import test from 'node:test';
import assert from 'node:assert/strict';
import { checkPatches } from '../scripts/plugin-runtime.mjs';

const source = (plugin, patches) => ({ plugin, patches });
const entry = (id, config = {}) => ({ id, name: './plugin.mjs', config });
const insert = (...entries) => ({ insert: entries });
const fails = (patches, contribution) => {
  assert.throws(() => checkPatches(patches), error =>
    error.message.includes('/one and /two') && error.message.includes(contribution));
};

test('independent inserts, groups, and disjoint entry fields compose without modifying input', () => {
  const patches = [source('/one', [insert(entry('one')), { id: 'core', disabled: false },
    { id: 'group', insert: [entry('child-one')] }]),
  source('/two', [insert(entry('two')), { id: 'core', inject: ['tools'] },
    { id: 'group', insert: [entry('child-two')] }])];
  const before = structuredClone(patches);
  checkPatches(patches);
  assert.deepEqual(patches, before);
  checkPatches([...patches].reverse());
});

test('duplicate inserted IDs including nested group rows identify both plugins', () => {
  fails([source('/one', [insert(entry('duplicate'))]),
    source('/two', [insert({ id: 'group', group: true, config: [entry('duplicate')] })])], 'inserted id duplicate');
});

test('entry config is atomic, so even disjoint config objects conflict', () => {
  fails([source('/one', [{ id: 'core', config: { a: 1 } }]),
    source('/two', [{ id: 'core', config: { b: 2 } }])], 'core.config');
});

test('a later plugin cannot replace an inserted row configuration or name guard', () => {
  fails([source('/one', [insert(entry('one', { a: 1 }))]),
    source('/two', [{ id: 'one', config: { a: 2 } }])], 'one.config');
  fails([source('/one', [insert(entry('one'))]),
    source('/two', [{ id: 'one', name: './different.mjs', disabled: true }])], 'one.name');
});

test('equal repeated field values agree; differing values and later changes fail', () => {
  checkPatches([source('/one', [{ id: 'core', config: { a: 1 } }]),
    source('/two', [{ id: 'core', config: { a: 1 } }])]);
  fails([source('/one', [{ id: 'core', disabled: false }]),
    source('/two', [{ id: 'core', disabled: true }])], 'core.disabled');
  fails([source('/one', [{ id: 'core', disabled: false }]),
    source('/two', [{ id: 'core', disabled: false }, { id: 'core', disabled: true }])], 'core.disabled');
});

test('group appends compose, but replacement versus append fails in either order', () => {
  const one = source('/one', [{ id: 'group', insert: [entry('child')] }]);
  const two = source('/two', [{ id: 'group', config: [] }]);
  fails([one, two], 'replacement versus group insert');
  assert.throws(() => checkPatches([two, one]), /replacement versus group insert/);
});

test('literal MCP server names must be unique across plugins even with distinct IDs', () => {
  fails([source('/one', [insert({ id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'same' } })]),
    source('/two', [insert({ id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'same' } })])], 'MCP serverName same');
  fails([source('/one', [insert({ name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'anonymous' } })]),
    source('/two', [insert({ name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'anonymous' } })])], 'MCP serverName anonymous');
});

test('MCP names reflect each plugin internal patch sequence', () => {
  const one = source('/one', [insert({ id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'original' } }),
    { id: 'mcp-one', config: { serverName: 'updated' } }]);
  fails([one, source('/two', [insert({ id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'updated' } })])],
    'MCP serverName updated');
  checkPatches([one, source('/two', [insert({ id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'original' } })])]);
});

test('group replacements remove nested and anonymous MCP children from name checks', () => {
  const one = source('/one', [insert({ id: 'group', group: true, config: [
    { id: 'nested', group: true, config: [
      { id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'removed' } },
      { name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'anonymous' } },
    ] },
  ] }), { id: 'group', config: [] }, { id: 'mcp-one', config: { serverName: 'detached' } }]);
  const two = source('/two', [insert(
    { id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'anonymous' } },
    { id: 'mcp-three', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'detached' } },
  )]);
  const before = structuredClone([one, two]);
  checkPatches([one, two]);
  assert.deepEqual([one, two], before);
});

test('group replacements remove children added by an earlier group append', () => {
  const one = source('/one', [insert({ id: 'group', group: true, config: [] }),
    { id: 'group', insert: [
      { id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'removed' } },
    ] }, { id: 'group', config: [] }]);
  const two = source('/two', [insert(
    { id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'removed' } },
  )]);
  const before = structuredClone([one, two]);
  checkPatches([one, two]);
  assert.deepEqual([one, two], before);
});

test('group replacement children participate in MCP name checks', () => {
  const one = source('/one', [insert({ id: 'group', group: true, config: [] }),
    { id: 'group', config: [{ group: true, config: [
      { name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'active' } },
    ] }] }]);
  fails([one, source('/two', [insert(
    { id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'active' } },
  )])], 'MCP serverName active');
});

test('later ID patches target the old detached child after a group replacement', () => {
  const one = source('/one', [insert({ id: 'group', group: true, config: [
    { id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'original' } },
  ] }), { id: 'group', config: [
    { id: 'mcp-one', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'active' } },
  ] }, { id: 'mcp-one', config: { serverName: 'detached' } }]);
  checkPatches([one, source('/two', [insert(
    { id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'detached' } },
  )])]);
  fails([one, source('/two', [insert(
    { id: 'mcp-two', name: '@deepseek-ai/dsh-mcp-client', config: { serverName: 'active' } },
  )])], 'MCP serverName active');
});

test('one plugin retains its internal sequential patch behavior and !!js stays literal', () => {
  checkPatches([source('/one', [{ id: 'core', disabled: true }, { id: 'core', disabled: false }])]);
  checkPatches([source('/one', [{ id: 'core', config: { value: { __jsExpr: 'ctx.environment.VALUE' } } }]),
    source('/two', [{ id: 'core', config: { value: { __jsExpr: 'ctx.environment.VALUE' } } }])]);
});
