import { test } from 'node:test'
import assert from 'node:assert/strict'
import { choices, fieldKind, initialInputs, inputErrors, inputKey, inputPointer, runLocation } from './workflow-inputs.js'

test('enum labels retain exact string, number, Boolean and null values', () => {
  const field = { enum: ['false', false, 0, null], x_ui: { enum_labels: ['Text', 'No', 'Zero', 'Nothing'] } }
  assert.deepEqual(choices(field).map(item => item.value), ['false', false, 0, null])
  assert.equal(choices(field)[1].label, 'No')
  assert.deepEqual(choices({ enum: ['false', false] }).map(item => item.label), ['false (string)', 'false (boolean)'])
  assert.equal(fieldKind({ type: 'boolean', enum: [false] }), 'choice')
  assert.deepEqual(choices({ oneOf: [{ const: 'weekly', title: 'Weekly summary' }] }), [{ value: 'weekly', label: 'Weekly summary' }])
})
test('defaults are annotations except explicitly prefilled required inputs; drafts remain independent', () => {
  const schema = { properties: { count: { type: 'integer', default: 7 }, enabled: { type: 'boolean', default: true } }, required: ['count'] }
  const manual = initialInputs(schema), schedule = initialInputs(schema, { enabled: false })
  assert.deepEqual(manual, { count: 7 })
  assert.deepEqual(schedule, { enabled: false, count: 7 })
  manual.count = 8
  assert.equal(schedule.count, 7)
})
test('false, zero, empty text, null and JSON shapes survive without guessing types', () => {
  const schema = { type: 'object', properties: { flag: { type: 'boolean' }, zero: { type: 'integer' }, empty: { type: 'string' }, nil: { type: ['string', 'null'] }, list: { type: 'array' }, nested: { type: 'object' } } }
  const value = { flag: false, zero: 0, empty: '', nil: null, list: [], nested: {} }
  assert.deepEqual(initialInputs(schema, value), value)
  assert.deepEqual(inputErrors(schema, value), {})
  assert.deepEqual(inputErrors(schema, { flag: 'false', zero: 1.1 }).zero, 'Enter a valid integer.')
})
test('unsupported schema shapes use an honest JSON fallback', () => {
  assert.equal(fieldKind({ type: 'object' }), 'json')
  assert.equal(fieldKind({ anyOf: [{ type: 'string' }, { type: 'number' }] }), 'json')
  assert.equal(fieldKind({ type: 'string', x_resource: { kind: 'agent' } }), 'resource')
})
test('input pointers and canonical run links escape authored keys and IDs', () => {
  assert.equal(inputKey(inputPointer('a/b~c')), 'a/b~c')
  assert.equal(runLocation('test_job', 'run/?x'), '/workflows/test_job/runs/run%2F%3Fx')
})
