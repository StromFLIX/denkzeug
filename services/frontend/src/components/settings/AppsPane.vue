<template>
  <h2 id="apps-library" class="settings__title">Apps</h2>
  <p class="settings__intro">Your private library of interactive HTML apps. Inspect source, try sample data, then publish a revision.</p>
  <p v-if="sandboxStatus" class="caption dim" role="status">{{ sandboxStatus }}</p>
  <div class="apps-actions">
    <button class="btn btn--primary" :disabled="busy" @click="create">Create app</button>
    <button class="btn" :disabled="busy" @click="importFile?.click()">Import package</button>
    <input ref="importFile" type="file" accept=".json,application/json" class="apps-file" aria-label="Import app package" @change="importPackage" />
    <label class="caption apps-check"><input v-model="showArchived" type="checkbox" /> Show archived</label>
  </div>
  <p v-if="error" class="caption field-hint--bad" role="alert">{{ error }} <button v-if="loadFailed" class="btn btn--sm" @click="loadLibrary">Retry loading</button></p>
  <p v-if="notice" class="caption" role="status">{{ notice }}</p>
  <p v-if="loading" class="caption dim" role="status">Loading apps…</p>
  <p v-else-if="!apps.length && !loadFailed" class="apps-empty">No apps yet. Create an app or import a JSON package to start. Nothing runs until you choose a preview.</p>
  <p v-else-if="!visibleApps.length && !loadFailed" class="caption dim">No active apps. Turn on Show archived to find archived apps.</p>
  <ul v-if="visibleApps.length" class="apps-list" aria-label="App library">
    <li v-for="item in visibleApps" :key="item.id">
      <button class="apps-item" :aria-pressed="selectedId === item.id" :disabled="busy" @click="openApp(item.id)">
        <span class="apps-item__name">{{ item.title }} <span v-if="item.archived" class="caption dim">Archived</span><span v-if="hasDraft(item.id)" class="caption">Unsaved draft</span></span>
        <span v-if="item.description" class="caption dim">{{ item.description }}</span>
        <span class="caption dim">{{ item.id }} · Revision {{ item.active_revision }}</span>
      </button>
    </li>
  </ul>
  <section id="apps-permissions" class="apps-permissions" aria-label="App permissions">
    <h3>Permissions</h3>
    <p class="caption dim">Apps run in an isolated preview. This release grants no network or tool access. Preview uses sample data only; it never runs a workflow. Imported code can be misleading: inspect its HTML before previewing it.</p>
  </section>
  <section id="apps-editor" class="apps-editor" aria-label="App source & revisions">
    <p v-if="detailLoading" class="caption dim" role="status">Loading app source…</p>
    <p v-else-if="!draft" class="caption dim">Select an app to inspect its source and revision history, or create a new app.</p>
    <template v-else>
      <div class="apps-editor__head">
        <h3>{{ selectedId ? summary?.title || selectedId : 'New app' }}</h3>
        <span class="caption" role="status">{{ historical ? `Viewing revision ${selectedRevision} (read-only)` : dirty ? 'Unsaved draft' : 'Published source' }}</span>
      </div>
      <p v-if="summary && usage" class="caption dim">{{ usage.saved_results }} saved workflow results<span v-if="usage.workflows?.length"> · {{ usage.workflows.join(', ') }}</span>. Results retain their original revision.</p>
      <p class="caption dim">Drafts stay in memory while this Apps pane is open. Leaving this Settings category or reloading discards unpublished changes.</p>
      <div v-if="summary" class="apps-history">
        <label for="apps-revision" class="caption">Revision history</label>
        <select id="apps-revision" class="field" :value="selectedRevision" :disabled="busy || revisionLoading" @change="selectRevision(Number($event.target.value))">
          <option v-for="item in revisions" :key="item.revision" :value="item.revision">Revision {{ item.revision }}{{ item.revision === summary.active_revision ? ' (active)' : '' }}{{ item.change_note ? ` — ${item.change_note}` : '' }}</option>
        </select>
        <button class="btn btn--sm" :disabled="busy" @click="refreshHistory">Refresh history</button>
        <button class="btn btn--sm" :disabled="busy" @click="archive">{{ summary.archived ? 'Unarchive app' : 'Archive app' }}</button>
      </div>
      <p v-if="reloadRequired" class="caption" role="alert">The app was published. Reload its source before making another change. <button class="btn btn--sm" :disabled="busy" @click="reloadPublished">Reload published source</button></p>
      <p v-if="summary?.archived" class="caption">Archived apps remain available here for inspection and export. Unarchive this app before publishing.</p>
      <p v-if="revisionLoading" class="caption dim" role="status">Loading revision…</p>
      <form v-else @submit.prevent="publish">
        <fieldset :disabled="busy" class="apps-fields">
          <div class="apps-grid">
            <label class="apps-label" for="apps-title">Title<input id="apps-title" v-model="displayed.title" class="field" :readonly="historical" required maxlength="120" /></label>
            <label class="apps-label" for="apps-slug">Slug<input id="apps-slug" v-model="displayed.id" class="field" :readonly="!!selectedId" required autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="apps-slug-help" /></label>
          </div>
          <p id="apps-slug-help" class="caption dim">A permanent identifier, such as <code>weekly-report</code>. Use lowercase letters, numbers and hyphens.</p>
          <label class="apps-label" for="apps-description">Description<textarea id="apps-description" v-model="displayed.description" class="field" :readonly="historical" rows="2" /></label>
          <label class="apps-label" for="apps-html">Self-contained HTML<textarea id="apps-html" v-model="displayed.html" class="field apps-code" :readonly="historical" rows="14" spellcheck="false" autocomplete="off" aria-describedby="apps-html-help" required /></label>
          <p id="apps-html-help" class="caption dim">Include HTML, styles and scripts in one file. Remote scripts, styles, fonts and other network resources are unavailable.</p>
          <div class="apps-grid">
            <label class="apps-label" for="apps-schema">Input schema (JSON object)<textarea id="apps-schema" v-model="displayed.input_schema" class="field apps-code" :readonly="historical" rows="7" spellcheck="false" /></label>
            <label class="apps-label" for="apps-sample">Sample data (JSON object)<textarea id="apps-sample" v-model="displayed.sample_data" class="field apps-code" :readonly="historical" rows="7" spellcheck="false" /></label>
          </div>
          <label v-if="!historical" class="apps-label" for="apps-note">Change note<input id="apps-note" v-model="displayed.change_note" class="field" placeholder="What changed in this revision?" maxlength="2000" /></label>
        </fieldset>
        <div class="apps-actions apps-publish">
          <button v-if="!historical" type="submit" class="btn btn--primary" :disabled="busy || reloadRequired || summary?.archived">{{ busy ? 'Saving…' : 'Publish new immutable revision' }}</button>
          <button v-else type="button" class="btn btn--primary" :disabled="busy || reloadRequired || summary?.archived || dirty" @click="restore">Restore as new revision</button>
          <button type="button" class="btn" :disabled="busy" @click="showPreview">Preview sample</button>
          <button type="button" class="btn" :disabled="busy" @click="exportPackage">Export JSON</button>
          <button v-if="dirty" type="button" class="btn" :disabled="busy" @click="discardDraft">Discard draft</button>
        </div>
      </form>
      <p v-if="historical && dirty" class="caption">Your active-revision draft is preserved. Discard that draft before restoring a prior revision, or select the active revision to keep editing.</p>
      <p v-if="historical" class="caption dim">Restoring copies this source to a new active revision. Existing revisions and their references stay unchanged.</p>
      <section v-if="preview" class="apps-preview" aria-label="Sample preview">
        <div class="apps-editor__head"><h3>Sample preview</h3><button class="btn btn--sm" @click="preview = null">Close preview</button></div>
        <p class="caption dim">Snapshot of the source and sample data when you clicked Preview sample. Publish separately to save changes.</p>
        <McpAppView :key="previewKey" :app="preview" />
      </section>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, request } from '../../api'
import { appDraft, appPayload, exportAppPackage, importAppPackage, MAX_APP_PACKAGE_BYTES } from '../../apps.js'
import McpAppView from '../McpAppView.vue'

const apps = ref([]), loading = ref(true), loadFailed = ref(false), error = ref(''), notice = ref(''), sandboxStatus = ref('')
const showArchived = ref(false), selectedId = ref(null), summary = ref(null), revisions = ref([])
const draft = ref(null), baseline = ref(''), selectedRevision = ref(null), historicalSource = ref(null)
const reloadRequired = ref(false), usage = ref(null)
const detailLoading = ref(false), revisionLoading = ref(false), saving = ref(false), preview = ref(null), previewKey = ref(0), importFile = ref(null)
const drafts = new Map()
let requestId = 0
const busy = computed(() => saving.value || detailLoading.value || revisionLoading.value)
const dirty = computed(() => draft.value && (!selectedId.value || JSON.stringify(draft.value) !== baseline.value))
const historical = computed(() => summary.value && selectedRevision.value !== summary.value.active_revision)
const displayed = computed(() => historical.value ? historicalSource.value : draft.value)
const visibleApps = computed(() => apps.value.filter(item => showArchived.value || !item.archived))
function remember () {
  if (draft.value) drafts.set(selectedId.value || '', { draft: draft.value, baseline: baseline.value })
}
function hasDraft (id) {
  if (selectedId.value === id) return dirty.value
  const cached = drafts.get(id)
  return cached && JSON.stringify(cached.draft) !== cached.baseline
}
function clearFeedback () { error.value = ''; notice.value = ''; preview.value = null }
async function loadLibrary () {
  loading.value = true; loadFailed.value = false; error.value = ''
  try { apps.value = (await api.apps()).apps || [] }
  catch (failure) { error.value = failure.message; loadFailed.value = true }
  finally { loading.value = false }
}
function create () {
  remember(); requestId++; clearFeedback(); reloadRequired.value = false
  selectedId.value = null; summary.value = null; revisions.value = []; selectedRevision.value = null
  const cached = drafts.get('')
  draft.value = cached?.draft || appDraft(); baseline.value = cached?.baseline || ''
}
function acceptDetail (data, id, preserveDraft = false) {
  if (!data.app || !data.revision || typeof data.revision.html !== 'string') throw new Error('App source is unavailable. Refresh history to try again.')
  summary.value = data.app
  usage.value = data.usage || null
  const source = { ...data.app, ...data.revision, id }
  const active = appDraft(source)
  revisions.value = data.revisions?.length ? data.revisions : [{ revision: data.app.active_revision }]
  selectedRevision.value = data.app.active_revision; historicalSource.value = null
  const cached = drafts.get(id)
  const keepCached = cached && JSON.stringify(cached.draft) !== cached.baseline
  draft.value = preserveDraft ? draft.value : keepCached ? cached.draft : active
  baseline.value = preserveDraft || !keepCached ? JSON.stringify(active) : cached.baseline
  reloadRequired.value = false
}
async function openApp (id) {
  remember(); clearFeedback(); detailLoading.value = true
  const ticket = ++requestId
  try {
    const data = await api.app(id)
    if (ticket !== requestId) return
    acceptDetail(data, id); selectedId.value = id
  } catch (failure) { if (ticket === requestId) error.value = failure.message }
  finally { if (ticket === requestId) detailLoading.value = false }
}
async function reloadPublished () {
  detailLoading.value = true; clearFeedback()
  try { acceptDetail(await api.app(selectedId.value), selectedId.value) }
  catch (failure) { error.value = failure.message }
  finally { detailLoading.value = false }
}
async function refreshHistory () {
  clearFeedback(); detailLoading.value = true
  try { acceptDetail(await api.app(selectedId.value), selectedId.value, true); notice.value = 'History refreshed. Your draft is unchanged; publishing uses the current active revision.' }
  catch (failure) { error.value = failure.message }
  finally { detailLoading.value = false }
}
async function selectRevision (revision) {
  clearFeedback()
  if (revision === summary.value.active_revision) { selectedRevision.value = revision; historicalSource.value = null; return }
  revisionLoading.value = true
  try {
    const source = await api.appRevision(selectedId.value, revision)
    if (typeof source.html !== 'string') throw new Error('Revision source is unavailable.')
    historicalSource.value = appDraft({ ...summary.value, ...source, id: selectedId.value })
    selectedRevision.value = revision
  } catch (failure) { error.value = failure.message }
  finally { revisionLoading.value = false }
}
async function save (payload) {
  saving.value = true; clearFeedback()
  let published = false
  try {
    await api.publishApp(payload); published = true; reloadRequired.value = true
    drafts.delete(selectedId.value || ''); drafts.delete(payload.id)
    selectedId.value = payload.id
    // Mark saved before reloading so a failed read never presents a retry as safe.
    baseline.value = JSON.stringify(draft.value)
    await loadLibrary()
    acceptDetail(await api.app(payload.id), payload.id)
    notice.value = `Published revision ${summary.value.active_revision}.`
  } catch (failure) { error.value = published ? `Published, but could not reload source: ${failure.message}. Refresh history before publishing again.` : failure.message }
  finally { saving.value = false }
}
async function publish () {
  if (reloadRequired.value || historical.value || busy.value || summary.value?.archived) return
  try { await save(appPayload(draft.value, summary.value?.active_revision || 0)) }
  catch (failure) { error.value = failure.message }
}
async function restore () {
  if (dirty.value || reloadRequired.value || busy.value || summary.value?.archived) return
  try {
    const payload = appPayload(historicalSource.value, summary.value.active_revision)
    payload.change_note = `Restored revision ${selectedRevision.value}`
    await save(payload)
  } catch (failure) { error.value = failure.message }
}
function discardDraft () {
  clearFeedback()
  drafts.delete(selectedId.value || '')
  if (!selectedId.value) { draft.value = appDraft(); baseline.value = ''; return }
  reloadPublished()
}
async function archive () {
  saving.value = true; clearFeedback()
  try {
    await api.editApp(selectedId.value, { archived: !summary.value.archived, expected_revision: summary.value.active_revision })
    summary.value = { ...summary.value, archived: !summary.value.archived }
    await loadLibrary()
    notice.value = summary.value.archived ? 'App archived. Its revision history is preserved.' : 'App unarchived.'
  } catch (failure) { error.value = failure.message }
  finally { saving.value = false }
}
function showPreview () {
  try {
    const source = appPayload(displayed.value)
    preview.value = { html: source.html, input: {}, result: { content: [], structuredContent: source.sample_data }, title: source.title }
    previewKey.value++; error.value = ''
  } catch (failure) { error.value = failure.message }
}
function exportPackage () {
  try {
    const source = appPayload(displayed.value)
    const url = URL.createObjectURL(new Blob([exportAppPackage(source)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `${source.id}.app.json`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    error.value = ''
  } catch (failure) { error.value = failure.message }
}
async function importPackage (event) {
  const file = event.target.files?.[0]
  if (!file) return
  try {
    if (file.size > MAX_APP_PACKAGE_BYTES) throw new Error('App packages must be 2 MB or smaller.')
    const imported = importAppPackage(await file.text())
    remember(); requestId++; clearFeedback(); reloadRequired.value = false; selectedId.value = null; summary.value = null; revisions.value = []; selectedRevision.value = null
    draft.value = imported; baseline.value = ''
    notice.value = 'Imported into an unsaved draft. Inspect the source before previewing or publishing. Choose a new slug to avoid replacing an existing app.'
  } catch (failure) { error.value = failure.message }
  finally { event.target.value = '' }
}
onMounted(() => {
  loadLibrary()
  request('/api/mcp-apps/runtime').then(config => {
    sandboxStatus.value = config.sandbox_url ? `Sandbox: ${config.sandbox_url}` : 'Preview requires a separate sandbox origin. Administrators: configure MCP_APPS_SANDBOX_URL and MCP_APPS_HOST_ORIGINS. Source management remains available.'
  }).catch(() => { sandboxStatus.value = 'Sandbox configuration could not be read. Source management remains available.' })
})
</script>

<style scoped>
.apps-actions, .apps-history, .apps-editor__head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.apps-actions { margin: 20px 0; }
.apps-check { display: inline-flex; align-items: center; gap: 7px; }
.apps-file { display: none; }
.apps-empty { padding: 24px 0; color: var(--text-muted); line-height: 1.7; max-width: 65ch; }
.apps-list { list-style: none; margin: 20px 0; padding: 0; border-top: 1px solid var(--border); }
.apps-list li { border-bottom: 1px solid var(--border); }
.apps-item { display: flex; flex-direction: column; gap: 7px; padding: 16px 12px; width: 100%; text-align: left; color: var(--text); background: transparent; border: 0; border-left: 3px solid transparent; cursor: pointer; font: inherit; overflow-wrap: anywhere; }
.apps-item:hover { background: var(--surface-hover); }
.apps-item[aria-pressed=true] { border-left-color: var(--accent); background: var(--accent-soft); }
.apps-item__name { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; font-weight: 600; }
.apps-permissions { padding: 16px 0; border-bottom: 1px solid var(--border); }
.apps-permissions p, .apps-editor p { max-width: 78ch; line-height: 1.65; }
.apps-permissions h3, .apps-editor h3 { font-size: 0.9375rem; margin: 0; }
.apps-editor { padding-top: 24px; min-width: 0; }
.apps-editor__head { justify-content: space-between; }
.apps-history { margin: 20px 0; }
.apps-history select { width: auto; max-width: 100%; min-width: 0; flex: 1 1 190px; }
.apps-fields { border: 0; padding: 0; margin: 0; min-width: 0; }
.apps-label { display: flex; flex-direction: column; gap: 8px; font-size: 0.8125rem; margin: 16px 0; min-width: 0; }
.apps-label .field { width: 100%; min-width: 0; box-sizing: border-box; }
textarea.field { resize: vertical; }
.apps-code { font-family: var(--font-mono); line-height: 1.6; tab-size: 2; }
.apps-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.apps-publish { border-top: 1px solid var(--border); padding-top: 20px; }
.apps-preview { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--border); min-width: 0; }
input[type=checkbox] { accent-color: var(--accent); }
@media (max-width: 640px) {
  .apps-grid { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .apps-actions > button { flex: 1 1 auto; white-space: normal; }
  .apps-check { width: 100%; }
  .apps-history > label { width: 100%; }
}
</style>
