<template>
  <div class="output-revisions">
    <label>Revision
      <select :value="modelValue ?? ''" @change="$emit('update:modelValue', Number($event.target.value) || null)">
        <option value="">Latest · {{ latest }}</option>
        <option v-if="modelValue !== null && !prior.includes(modelValue)" :value="modelValue">Revision {{ modelValue }}</option>
        <option v-for="revision in prior" :key="revision" :value="revision">Revision {{ revision }}</option>
      </select>
    </label>
    <button v-if="(latest > 1 || modelValue !== null) && (!prior.length || error)" type="button" class="btn btn--sm btn--outline" :disabled="loading" @click="$emit('load')">
      {{ loading ? 'Loading…' : 'Load revision history' }}
    </button>
    <p v-if="error" class="caption" role="alert">{{ error }}</p>
  </div>
</template>

<script setup>
defineProps({
  modelValue: { type: Number, default: null },
  latest: { type: Number, required: true },
  prior: { type: Array, required: true },
  loading: Boolean,
  error: String
})
defineEmits(['update:modelValue', 'load'])
</script>

<style scoped>
.output-revisions { display: flex; align-items: center; justify-content: space-between; gap: 8px 12px; flex-wrap: wrap; padding-block: 14px; border-bottom: 1px solid var(--border); }
.output-revisions label { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--text-muted); font-size: 0.75rem; }
.output-revisions select { max-width: 100%; min-height: 44px; padding: 6px 8px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-input); color: var(--text); font: inherit; }
.output-revisions button { min-height: 44px; }
.output-revisions p { flex-basis: 100%; margin: 0; }
</style>
