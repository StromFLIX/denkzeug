<template>
  <div>
    <q-select :for="id" :model-value="modelValue" :options="options" :aria-label="label"
      outlined dense use-input emit-value map-options option-value="value" option-label="label"
      :display-value="selected?.label || (modelValue ? (checking ? 'Checking selection…' : 'Unavailable selection') : 'Search by name')"
      :input-debounce="200" :loading="loading || checking" @filter="search" @update:model-value="choose">
      <template #option="scope">
        <q-item v-bind="scope.itemProps">
          <q-item-section>
            <q-item-label>{{ scope.opt.label }}</q-item-label>
            <q-item-label caption>{{ scope.opt.description || `${kind === 'chat' ? 'Your chat' : 'Saved agent'} · …${scope.opt.value.slice(-6)}` }}</q-item-label>
          </q-item-section>
        </q-item>
      </template>
      <template #after-options>
        <button v-if="nextOffset != null" type="button" class="pick-menu__item" :disabled="loading" @click.stop="loadMore">Load more results</button>
      </template>
      <template #no-option><q-item><q-item-section>{{ error || 'No matching resources.' }}</q-item-section></q-item></template>
    </q-select>
    <p v-if="error" class="resource-error" role="alert">{{ error }} <button class="btn btn--sm" type="button" @click="retry">Retry</button></p>
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
const props = defineProps({ modelValue: String, workflow: String, field: String, label: String, id: String, kind: String })
const emit = defineEmits(['update:modelValue', 'availability'])
const items = ref([]), selected = ref(null), loading = ref(false), checking = ref(false), error = ref(''), nextOffset = ref(null)
let query = '', version = 0, controller, resolveController, resolveVersion = 0
const options = computed(() => selected.value && !items.value.some(item => item.value === selected.value.value) ? [selected.value, ...items.value] : items.value)
function choose (value) {
  selected.value = options.value.find(item => item.value === value) || null
  emit('availability', Boolean(selected.value))
  emit('update:modelValue', value)
}
async function resolve () {
  const token = ++resolveVersion
  resolveController?.abort()
  if (selected.value?.value !== props.modelValue) selected.value = null
  error.value = ''; checking.value = false
  if (!props.modelValue) { emit('availability', true); return }
  checking.value = true
  emit('availability', false)
  resolveController = new AbortController()
  try {
    const data = await api.workflowInputOptions(props.workflow, props.field, { selected: props.modelValue }, resolveController.signal)
    if (token !== resolveVersion) return
    selected.value = data.selected
    emit('availability', Boolean(data.selected))
    if (!data.selected) error.value = 'This selection is unavailable. Choose another.'
  } catch (failure) {
    if (token === resolveVersion && failure.name !== 'AbortError') error.value = 'Could not verify the selection. Retry or choose another.'
  } finally { if (token === resolveVersion) checking.value = false }
}
function retry () { resolve(); fetchOptions(false) }
async function search (value, update, abort) {
  query = value
  await fetchOptions(false, update, abort)
}
async function fetchOptions (more, update = callback => callback(), abort = () => {}) {
  const token = ++version
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const data = await api.workflowInputOptions(props.workflow, props.field, { q: query, offset: more ? nextOffset.value : 0 }, controller.signal)
    if (token !== version) return
    update(() => { items.value = more ? [...items.value, ...data.items] : data.items; nextOffset.value = data.next_offset })
  } catch (failure) {
    if (token === version && failure.name !== 'AbortError') { error.value = 'Could not load choices. Retry.'; abort() }
  } finally { if (token === version) loading.value = false }
}
function loadMore () { fetchOptions(true) }
watch(() => [props.workflow, props.field, props.modelValue], () => {
  version++; controller?.abort(); items.value = []; nextOffset.value = null; loading.value = false
  resolve()
}, { immediate: true })
onUnmounted(() => { version++; resolveVersion++; controller?.abort(); resolveController?.abort() })
</script>

<style scoped>
.resource-error { font-size: 0.75rem; color: var(--danger); margin: 6px 0; }
</style>
