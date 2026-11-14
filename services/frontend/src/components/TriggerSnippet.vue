<template>
  <div class="trigger">
    <p>External triggers need a scoped automation credential. Account sessions cannot be copied into webhook links or scripts.</p>
    <p class="muted caption">Run this workflow from its Run controls. Keep the input below for your integration.</p>
    <pre class="trigger__code mono">{{ JSON.stringify(inputs, null, 2) }}</pre>
  </div>
</template>
<script setup>
defineProps({ workflow: { type: String, required: true }, inputs: { type: Object, default: () => ({}) } })
</script>
<style scoped>
.trigger { padding: 1rem; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); }
.trigger__code { white-space: pre; overflow-x: auto; padding: 1rem; background: var(--surface-code); }
</style>
