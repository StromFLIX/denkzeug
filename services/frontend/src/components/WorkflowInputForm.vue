<template>
  <div class="workflow-input-form">
    <p v-if="!Object.keys(properties).length" class="caption dim">This workflow needs no inputs.</p>
    <div v-for="(field, key) in properties" :key="key" class="input-field">
      <div class="input-field__head">
        <label :for="`${id}-${key}`">{{ field.title || key }}</label>
        <span class="caption dim">{{ typeLabel(field) }} · {{ required(key) ? 'Required' : 'Optional' }}</span>
      </div>
      <select v-if="['choice', 'boolean'].includes(fieldKind(field))" class="field" :id="`${id}-${key}`"
        :value="selectValue(key, field)" :aria-invalid="Boolean(errors[key])" :aria-describedby="`${id}-${key}-help`"
        @change="setChoice(key, field, $event.target.value)">
        <option v-if="!required(key)" value="omit">{{ omission(field) }}</option>
        <option v-else value="unset" disabled>Choose a value</option>
        <option v-if="nullable(field) && !choices(field)" value="null">Set null</option>
        <template v-if="fieldKind(field) === 'boolean'">
          <option value="true">Yes (true)</option><option value="false">No (false)</option>
        </template>
        <option v-for="(option, index) in choices(field) || []" :key="index" :value="`choice-${index}`">{{ option.label }}</option>
      </select>
      <template v-else>
        <WorkflowResourceField v-if="fieldKind(field) === 'resource' && mode(key) !== 'null'" :id="`${id}-${key}`"
          :workflow="workflow" :field="inputPointer(key)" :label="field.title || key" :kind="field.x_resource.kind"
          :model-value="modelValue[key]" @update:model-value="set(key, $event)" @availability="resourceAvailable[key] = $event" />
        <textarea v-else-if="fieldKind(field) === 'json' && mode(key) !== 'null'" class="field input-json" :id="`${id}-${key}`"
          :value="jsonDrafts[key] ?? (owns(modelValue, key) ? JSON.stringify(modelValue[key], null, 2) : '')"
          :aria-invalid="Boolean(errors[key])" :aria-describedby="`${id}-${key}-help`" @input="setJSON(key, $event.target.value)" />
        <input v-else :id="`${id}-${key}`" class="field" :type="['integer', 'number'].includes(fieldKind(field)) ? 'number' : 'text'"
          :value="modelValue[key] ?? ''" :disabled="mode(key) === 'null'"
          :step="fieldKind(field) === 'integer' ? 1 : 'any'" :min="field.minimum" :max="field.maximum"
          :placeholder="owns(field, 'default') ? `Default: ${String(field.default)}` : ''"
          :aria-invalid="Boolean(errors[key])" :aria-describedby="`${id}-${key}-help`"
          @input="set(key, ['integer', 'number'].includes(fieldKind(field)) ? ($event.target.value === '' ? undefined : Number($event.target.value)) : $event.target.value)" />
        <select v-if="!required(key) || nullable(field)" class="field input-mode" :aria-label="`${field.title || key} value mode`"
          :value="mode(key)" @change="setMode(key, field, $event.target.value)">
          <option v-if="!required(key)" value="omit">{{ omission(field) }}</option>
          <option value="value">Set value</option><option v-if="nullable(field)" value="null">Set null</option>
        </select>
      </template>
      <p :id="`${id}-${key}-help`" class="input-help">
        {{ field.description }}<span v-if="fieldKind(field) === 'json'"> Enter JSON for this structured input. It is validated by the server.</span>
        <span v-if="field.minimum !== undefined"> Minimum: {{ field.minimum }}.</span><span v-if="field.maximum !== undefined"> Maximum: {{ field.maximum }}.</span>
        <span v-if="owns(field, 'default')"> Workflow default: {{ JSON.stringify(field.default) }}. Leaving it not supplied omits the key.</span>
      </p>
      <p v-if="errors[key]" :id="`${id}-${key}-error`" class="input-error" role="alert">{{ errors[key] }}</p>
    </div>
    <p v-if="rawError && !editingRaw" class="input-error" role="alert">{{ rawError }} Open Advanced input JSON to correct it.</p>
    <details class="advanced-inputs" @toggle="openedJSON">
      <summary>Advanced input JSON</summary>
      <textarea v-model="rawJSON" class="field input-json" aria-label="Advanced input JSON" @input="parseRaw" />
      <p v-if="rawError" class="input-error" role="alert">{{ rawError }}</p>
    </details>
  </div>
</template>
<script setup>
import { computed, ref, useId, watch } from 'vue'
import WorkflowResourceField from './WorkflowResourceField.vue'
import { baseType, choices, fieldKind, inputErrors, inputPointer, nullable, owns } from '../workflow-inputs'
const props = defineProps({ schema: { type: Object, default: () => ({}) }, modelValue: { type: Object, default: () => ({}) }, workflow: String, serverErrors: { type: Object, default: () => ({}) } })
const emit = defineEmits(['update:modelValue'])
const id = useId(), localErrors = ref({}), jsonDrafts = ref({}), jsonErrors = ref({}), resourceAvailable = ref({})
const rawJSON = ref('{}'), rawError = ref(''), editingRaw = ref(false)
const properties = computed(() => props.schema.properties || {})
const errors = computed(() => ({ ...props.serverErrors, ...localErrors.value, ...jsonErrors.value }))
const required = key => (props.schema.required || []).includes(key)
const typeLabel = field => field.x_resource?.kind || baseType(field) || (choices(field) ? 'Choice' : 'JSON')
const omission = field => owns(field, 'default') ? `Use workflow default (${JSON.stringify(field.default)})` : 'Not supplied'
const mode = key => !owns(props.modelValue, key) || props.modelValue[key] === undefined ? 'omit' : props.modelValue[key] === null ? 'null' : 'value'
function set (key, value) {
  const next = { ...props.modelValue }
  if (value === undefined) delete next[key]
  else Object.defineProperty(next, key, { value, enumerable: true, writable: true, configurable: true })
  delete localErrors.value[key]
  emit('update:modelValue', next)
}
function setMode (key, field, value) {
  if (value === 'omit') { delete jsonDrafts.value[key]; delete jsonErrors.value[key]; set(key, undefined) }
  else if (value === 'null') { delete jsonDrafts.value[key]; delete jsonErrors.value[key]; set(key, null) }
  else if (mode(key) !== 'value') set(key, field.default ?? (fieldKind(field) === 'string' ? '' : undefined))
}
function selectValue (key, field) {
  if (mode(key) === 'omit') return required(key) ? 'unset' : 'omit'
  const values = choices(field)
  if (values) {
    const index = values.findIndex(item => JSON.stringify(item.value) === JSON.stringify(props.modelValue[key]))
    return mode(key) === 'null' && index === -1 ? 'null' : `choice-${index}`
  }
  return mode(key) === 'null' ? 'null' : String(props.modelValue[key])
}
function setChoice (key, field, value) {
  if (value === 'omit') set(key, undefined)
  else if (value === 'null') set(key, null)
  else if (value.startsWith('choice-')) set(key, choices(field)[Number(value.slice(7))].value)
  else set(key, value === 'true')
}
function setJSON (key, text) {
  jsonDrafts.value[key] = text
  try { const value = JSON.parse(text); delete jsonErrors.value[key]; set(key, value) }
  catch { jsonErrors.value[key] = 'Enter valid JSON.' }
}
function openedJSON (event) { editingRaw.value = event.target.open; if (event.target.open && !rawError.value) rawJSON.value = JSON.stringify(props.modelValue, null, 2) }
function parseRaw () {
  try {
    const value = JSON.parse(rawJSON.value)
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error()
    rawError.value = ''; jsonDrafts.value = {}; jsonErrors.value = {}; localErrors.value = {}
    emit('update:modelValue', value)
  } catch { rawError.value = 'Enter a valid JSON object. Your last valid inputs are preserved.' }
}
function validate () {
  localErrors.value = inputErrors(props.schema, props.modelValue)
  for (const [key, field] of Object.entries(properties.value)) {
    if (fieldKind(field) === 'resource' && mode(key) === 'value' && resourceAvailable.value[key] !== true) localErrors.value[key] = 'Verify this selection or choose another.'
  }
  return !Object.keys(localErrors.value).length && !Object.keys(jsonErrors.value).length && !rawError.value
}
watch(() => props.modelValue, value => { if (!editingRaw.value) rawJSON.value = JSON.stringify(value, null, 2) })
defineExpose({ validate })
</script>
<style scoped>
.input-field { margin: 0 0 1.1rem; }
.input-field__head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.input-field__head label { font-size: 0.875rem; font-weight: 500; }
.input-field__head .caption { text-transform: capitalize; font-size: 0.6875rem; }
.input-help { margin: 7px 0 0; font-size: 0.75rem; line-height: 1.6; color: var(--text-muted); }
.input-error { margin: 7px 0 0; font-size: 0.75rem; color: var(--danger); }
.input-mode { display: block; width: auto; max-width: 100%; margin: 7px 0 0 auto; min-height: 30px; padding-block: 4px; font-size: 0.6875rem; color: var(--text-muted); }
.input-json { min-height: 110px; font-family: var(--font-mono); resize: vertical; }
.advanced-inputs { border-top: 1px solid var(--border); padding-top: 14px; }
.advanced-inputs summary { cursor: pointer; font-size: 0.8125rem; color: var(--text-muted); padding-bottom: 12px; }
[aria-invalid='true'] { border-color: var(--danger); }
@media (pointer: coarse) { .field { font-size: 1rem; min-height: 44px; } }
@media (max-width: 560px) { .input-field__head { flex-wrap: wrap; } }
</style>
