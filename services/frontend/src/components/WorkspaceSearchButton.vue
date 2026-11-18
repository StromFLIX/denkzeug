<template>
  <button type="button" class="workspace-search" aria-haspopup="dialog" @click="openSearch(scope, options)">
    <span class="material-icons" aria-hidden="true">search</span>
    <span class="grow truncate">{{ label }}</span>
    <kbd v-if="shortcut" aria-hidden="true">{{ shortcut }}</kbd>
  </button>
</template>

<script setup>
import { useWorkspaceSearch } from '../workspace-search.js'

defineProps({ label: String, scope: String, shortcut: String, options: Object })
const openSearch = useWorkspaceSearch()
</script>

<style scoped>
.workspace-search { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; min-height: 44px; padding: 6px 10px; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface-raised); color: var(--text-muted); font: inherit; font-size: .8125rem; text-align: left; cursor: pointer; }
.workspace-search:active { background: var(--surface-hover); }
.workspace-search .material-icons { flex: none; font-size: 1.0625rem; }
@media (hover: hover) and (pointer: fine) { .workspace-search:hover { border-color: var(--accent); color: var(--text); } }
@media (pointer: coarse) { .workspace-search kbd { display: none; } }
</style>
