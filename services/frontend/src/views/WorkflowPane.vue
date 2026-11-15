<template>
  <div v-if="!name && route.query.view === 'runs'" class="stack grow process-pane">
    <header class="pane-head"><button class="btn btn--icon" aria-label="Back to workflows" @click="backTo('/workflows')"><span class="material-icons">arrow_back</span></button><div class="pane-head__title">All runs</div></header>
    <div class="pane-body scroll-y grow"><WorkflowRuns /></div>
  </div>
  <EmptyState v-else-if="!name" icon="account_tree" tone="workflow" :title="store.workflows.length ? 'Choose a workflow' : 'Make a process repeatable'"
    :description="store.workflows.length ? 'Workflows are repeatable processes. Select one to inspect its steps, run it, or set a schedule.' : 'Describe a task in chat and ask your agent to turn it into a workflow. Run it when you need it, or give it a schedule.'"
    to="/chats" action="Open chats" />

  <!-- a draft waiting for a human -->
  <div v-else-if="draft" class="stack grow process-pane">
    <header class="pane-head draft-head" data-skin-part="header">
      <button class="btn btn--icon pane-head__back" aria-label="Back to workflows" @click="backTo('/workflows')">
        <span class="material-icons">arrow_back</span>
      </button>
      <div class="grow">
        <div class="pane-head__title truncate">{{ draft.name }}</div>
        <div class="caption dim truncate">{{ draft.meta?.message || 'Draft' }}</div>
      </div>
      <span class="chip" :class="validation?.valid ? 'chip--success' : 'chip--danger'">
        {{ validation?.valid ? 'validates' : 'has errors' }}
      </span>
      <button class="btn btn--danger btn--sm" @click="discard">Discard</button>
      <button
        class="btn btn--primary btn--sm" :disabled="!validation?.valid || approving"
        @click="approve"
      >
        {{ approving ? 'Deploying…' : 'Approve and deploy' }}
      </button>
    </header>

    <nav class="tabs" aria-label="Draft views">
      <button v-for="item in ['Flow', 'Code diff']" :key="item" class="tab" :class="{ 'tab--active': tab === item }" @click="tab = item">{{ item }}</button>
    </nav>
    <WorkflowGraph v-if="tab === 'Flow'" :graph="draftGraph">
      <template #source>
        <select v-model="draftMode" class="field flow-picker" aria-label="Draft comparison version">
          <option value="changes">{{ draft.previous_graph ? 'Changes' : 'New workflow' }}</option>
          <option value="proposed">Proposed</option>
          <option v-if="draft.previous_graph" value="deployed">Deployed</option>
        </select>
      </template>
    </WorkflowGraph>
    <div v-else class="pane-body scroll-y grow">
      <section v-if="validation?.steps?.length" class="block">
        <div class="row" style="flex-wrap: wrap">
          <span
            v-for="step in validation.steps" :key="step.step"
            class="chip" :class="step.ok ? 'chip--success' : 'chip--danger'"
          >{{ step.step }}</span>
        </div>
        <p v-for="error in validation.errors || []" :key="error" class="caption" style="color: var(--danger)">
          {{ error }}
        </p>
        <p v-for="note in validation.warnings || []" :key="note" class="caption" style="color: var(--warning)">
          {{ note }}
        </p>
      </section>

      <section class="block">
        <div class="section-label">Diff</div>
        <pre class="code" style="margin-top: 8px"><span
          v-for="(line, index) in diffLines(draft.diff)" :key="index" :class="line.cls"
        >{{ line.text }}
</span></pre>
      </section>
    </div>
  </div>

  <!-- a published workflow -->
  <div v-else-if="workflow" class="stack grow process-pane">
    <header class="pane-head" data-skin-part="header">
      <button class="btn btn--icon pane-head__back" aria-label="Back to workflows" @click="backTo('/workflows')">
        <span class="material-icons">arrow_back</span>
      </button>
      <div class="avatar-sq" aria-hidden="true">
        <span class="material-icons">account_tree</span>
      </div>
      <div class="grow">
        <div class="pane-head__title truncate">{{ workflow.title || workflow.name }}</div>
        <div class="caption dim truncate">{{ workflow.description || workflow.name }}</div>
      </div>
      <span v-if="disabled" class="chip chip--warning">disabled</span>
      <span
        v-else-if="workflow.schedule" class="chip chip--accent"
        :title="`${workflow.schedule.description} · ${workflow.schedule.timezone}`"
      >
        <span class="material-icons" style="font-size: 0.8125rem">schedule</span>
        {{ workflow.schedule.next_run
          ? `Next ${scheduleTime(workflow.schedule.next_run, workflow.schedule.timezone)}`
          : workflow.schedule.description }}
      </span>
      <button ref="runTrigger" class="btn btn--primary btn--sm run-action" aria-label="Run workflow" :disabled="disabled" @click="openRun">
        <span class="material-icons" aria-hidden="true">play_arrow</span><span class="run-action-label">Run workflow</span>
      </button>
      <button v-if="account.user?.role === 'admin'" class="btn btn--icon" aria-label="Workflow actions">
        <span class="material-icons" aria-hidden="true">more_vert</span>
        <q-menu anchor="bottom right" self="top right" class="pick-menu">
          <button class="pick-menu__item" :disabled="togglingDisabled" @click="toggleDisabled">
            <span class="material-icons pick__icon" aria-hidden="true">{{ disabled ? 'play_circle' : 'pause_circle' }}</span>
            {{ disabled ? 'Enable' : 'Disable' }}
          </button>
          <button class="pick-menu__item" @click="remove">
            <span class="material-icons pick__icon" style="color: var(--danger)">delete</span>Delete
          </button>
        </q-menu>
      </button>
    </header>

    <section v-if="workflowControlError" class="notice" role="alert">
      <span class="material-icons" aria-hidden="true">warning</span>
      <span class="grow">{{ workflowControlError }}</span>
      <button class="btn btn--sm btn--outline" :disabled="togglingDisabled" @click="syncDisabled()">Retry sync</button>
    </section>
    <nav class="tabs" aria-label="Workflow views">
      <button
        v-for="item in tabs" :key="item" class="tab"
        :class="{ 'tab--active': tab === item }" @click="tab = item"
      >{{ item }}</button>
    </nav>

    <nav v-if="tab === 'Definition'" class="tabs definition-tabs" aria-label="Definition views">
      <button v-for="item in ['Flow', 'Code']" :key="item" class="tab" :class="{ 'tab--active': definitionTab === item }" @click="definitionTab = item">{{ item }}</button>
    </nav>
    <template v-if="tab === 'Definition' && definitionTab === 'Flow'">
      <ExecutionFlow v-if="selectedRun" :key="selectedRun" :workflow-id="selectedRun">
        <template #source>
          <select v-model="selectedRun" class="field flow-picker" aria-label="Flow source">
            <option value="">Definition</option>
            <option v-for="entry in workflow.runs || []" :key="entry.workflow_id" :value="entry.workflow_id">{{ entry.status }} - {{ fullTime(entry.created_at) }}</option>
          </select>
        </template>
      </ExecutionFlow>
      <WorkflowGraph v-else :key="name" :graph="workflow.graph">
        <template #source>
          <select v-model="selectedRun" class="field flow-picker" aria-label="Flow source">
            <option value="">Definition</option>
            <option v-for="entry in workflow.runs || []" :key="entry.workflow_id" :value="entry.workflow_id">{{ entry.status }} - {{ fullTime(entry.created_at) }}</option>
          </select>
        </template>
      </WorkflowGraph>
    </template>
    <div v-else class="pane-body scroll-y grow">
      <template v-if="tab === 'Overview'">
        <section class="block overview-intro"><h2>A process, and everything it has done.</h2><p class="block__intro">Run once, inspect the result, or leave it on a schedule.</p></section>
        <section class="block overview-schedule">
          <div><h2 class="section-label">Next execution</h2>
            <p v-if="workflow.schedule_status === 'unavailable'">Schedule unavailable. Retry when the scheduler is ready.</p>
            <p v-else-if="disabled">Disabled. Existing executions continue.</p>
            <template v-else-if="workflow.schedule"><strong>{{ workflow.schedule.next_run ? scheduleTime(workflow.schedule.next_run, workflow.schedule.timezone) : workflow.schedule.description }}</strong><p class="caption dim">{{ workflow.schedule.timezone }}</p></template>
            <p v-else class="caption dim">{{ account.user?.role === 'admin' ? 'Run when needed. No schedule is saved.' : 'Schedule configuration is managed by an administrator.' }}</p>
          </div>
          <button v-if="account.user?.role === 'admin'" class="btn btn--outline" @click="tab = 'Settings'">Edit schedule</button>
        </section>
        <section class="block"><h2 class="section-label">Latest execution</h2>
          <div v-if="latestRun" class="latest-run">
            <span class="chip" :class="`chip--${RUN_TONE[latestRun.status] || ''}`">{{ latestRun.status }}</span>
            <p class="caption dim">{{ fullTime(latestRun.created_at) }} · {{ latestRun.trigger }}</p>
            <p v-if="latestSummary" class="latest-summary">{{ latestSummary }}</p>
            <RouterLink class="btn btn--outline" :to="runLocation(name, latestRun.workflow_id)">View result</RouterLink>
          </div>
          <p v-else class="caption dim">No runs yet. Run this workflow to create its first execution record.</p>
        </section>
        <section class="block block--wide"><WorkflowRuns :workflow="name" preview @run="openRun" /></section>
      </template>
      <template v-else-if="tab === 'Settings'">
        <section v-if="disabled" class="notice">
          <span class="material-icons">pause_circle</span>
          <span class="grow">Disabled — new manual and webhook runs are blocked. Its schedule is paused when synchronized; existing runs continue.</span>
          <button v-if="account.user?.role === 'admin'" class="btn btn--sm btn--outline" :disabled="togglingDisabled" @click="toggleDisabled">Enable</button>
        </section>

        <p v-if="account.user?.role !== 'admin'" class="block__intro">Persistent workflow settings are managed by an administrator.</p>

        <section v-if="account.user?.role === 'admin'" class="block schedule-block">
          <h2 class="section-label">Schedule</h2>
          <p class="block__intro">Choose when this process repeats and the timezone it follows.</p>
          <p v-if="workflow.schedule_status === 'unavailable'" class="notice" role="alert">Schedule state is unavailable. Do not assume this workflow is unscheduled.<button class="btn btn--sm" @click="refreshSchedule">Retry</button></p>

          <div v-if="workflow.schedule" class="schedule-current">
            <span class="material-icons schedule-current__icon">event_repeat</span>
            <div class="schedule-current__rule">
              <strong>{{ workflow.schedule.description }}</strong>
              <span class="caption dim">{{ workflow.schedule.timezone }}</span>
            </div>
            <div class="schedule-current__next">
              <span class="caption dim">Next run</span>
              <strong>{{ workflow.schedule.next_run
                ? scheduleTime(workflow.schedule.next_run, workflow.schedule.timezone)
                : 'Calculating…' }}</strong>
            </div>
          </div>

          <div v-if="scheduleFrequency !== 'custom'" class="schedule-grid">
            <label class="schedule-field">
              <span>Repeat</span>
              <select v-model="scheduleFrequency" class="field">
                <option value="hourly">Every hour</option>
                <option value="daily">Every day</option>
                <option value="weekly">Selected days</option>
                <option value="monthly">Every month</option>
              </select>
            </label>

            <label v-if="scheduleFrequency === 'hourly'" class="schedule-field">
              <span>At minute</span>
              <input v-model.number="scheduleMinute" class="field" type="number" min="0" max="59" />
            </label>
            <label v-else class="schedule-field">
              <span>At</span>
              <input v-model="scheduleAt" class="field" type="time" required />
            </label>

            <label v-if="scheduleFrequency === 'monthly'" class="schedule-field">
              <span>Day of month</span>
              <input v-model.number="scheduleMonthDay" class="field" type="number" min="1" max="31" />
            </label>

            <label class="schedule-field schedule-field--timezone">
              <span>Timezone</span>
              <input v-model.trim="scheduleTimezone" class="field" list="workflow-timezones" autocomplete="off" />
              <datalist id="workflow-timezones">
                <option v-for="zone in timezones" :key="zone" :value="zone" />
              </datalist>
            </label>
          </div>

          <div v-if="scheduleFrequency === 'weekly'" class="schedule-days">
            <span class="schedule-days__label">Run on</span>
            <div class="segmented" aria-label="Days of the week">
              <button
                v-for="day in weekDays" :key="day.value" type="button" class="segment schedule-day"
                :class="{ 'segment--active': scheduleDays.includes(day.value) }"
                :aria-pressed="scheduleDays.includes(day.value)"
                @click="toggleScheduleDay(day.value)"
              >{{ day.label }}</button>
            </div>
          </div>

          <section class="scheduled-inputs">
            <h3 class="section-label">Inputs for scheduled runs</h3>
            <p class="block__intro">These inputs are independent of manual runs.</p>
            <p v-if="hasResourceInputs" class="notice">Resource fields currently support manual runs only. Unattended resource authorization is not yet available.</p>
            <WorkflowInputForm ref="scheduleForm" :key="`${name}-schedule`" v-model="scheduleInputs" :schema="inputSchema" :workflow="name" :server-errors="scheduleErrors" />
          </section>
          <div class="row schedule-actions">
            <button
              v-if="scheduleFrequency === 'custom'" class="btn btn--primary"
              :disabled="disabled || scheduling" @click="beginScheduleReplacement"
            >
              <span class="material-icons" style="font-size: 1.0625rem">edit_calendar</span>
              Replace schedule
            </button>
            <button
              v-else
              class="btn btn--primary" :disabled="disabled || scheduling || !scheduleReady"
              @click="schedule"
            >
              <span class="material-icons" style="font-size: 1.0625rem" aria-hidden="true">event_repeat</span>
              {{ scheduling ? 'Saving…' : workflow.schedule ? 'Update schedule' : 'Save schedule' }}
            </button>
            <button v-if="workflow.schedule" class="btn btn--danger" :disabled="scheduling" @click="unschedule">
              <span class="material-icons" style="font-size: 1.0625rem">event_busy</span>
              Remove schedule
            </button>
          </div>
        </section>

        <section v-if="account.user?.role === 'admin'" class="block">
          <h2 class="section-label">Results go to</h2>
          <p class="block__intro">Keep results together, or start a separate conversation for each run.</p>
          <div class="row segmented" style="margin-top: 8px">
            <button
              v-for="option in chatModes" :key="option.value" class="segment"
              :class="{ 'segment--active': chatMode === option.value }"
              @click="setChatMode(option.value)"
            >{{ option.label }}</button>
          </div>
        </section>

        <section v-if="account.user?.role === 'admin'" class="block">
          <h2 class="section-label">Trigger</h2>
          <p class="block__intro">Start this workflow from another tool. This preview uses the schedule input draft, not a manual run draft.</p>
          <TriggerSnippet class="block__panel" :workflow="workflow.name" :inputs="scheduleInputs" />
        </section>
      </template>

      <section v-else-if="tab === 'Definition' && definitionTab === 'Code'" class="block block--wide">
        <CodeViewer :files="files" />
      </section>

      <section v-else-if="tab === 'Runs'" class="block block--wide"><WorkflowRuns :workflow="name" @run="openRun" /></section>
    </div>
    <q-dialog v-model="runOpen" persistent no-route-dismiss @escape-key="closeRun">
      <div class="run-dialog" role="dialog" aria-labelledby="manual-run-title">
        <header class="run-dialog__head"><div><h2 id="manual-run-title">Run {{ workflow.title || workflow.name }}</h2><p>Run once. Your scheduled inputs will not change.</p></div><button class="btn btn--icon" aria-label="Close run form" :disabled="running" @click="closeRun"><span class="material-icons">close</span></button></header>
        <div class="run-dialog__body scroll-y">
          <div v-if="runError" class="notice" role="alert">{{ runError }}</div>
          <WorkflowInputForm ref="manualForm" :key="`${name}-manual`" v-model="inputs" :schema="inputSchema" :workflow="name" :server-errors="runErrors" />
        </div>
        <footer class="run-dialog__footer"><button v-if="workflow.schedule?.input" class="btn btn--outline" :disabled="running" @click="copyScheduledInputs">Use scheduled inputs</button><span class="grow" /><button class="btn btn--outline" :disabled="running" @click="closeRun">Cancel</button><button class="btn btn--primary" :disabled="running || disabled" @click="run">{{ running ? 'Starting…' : 'Run now' }}</button></footer>
      </div>
    </q-dialog>
  </div>

  <div v-else class="empty">
    <span>{{ loadError || 'Loading...' }}</span>
    <button v-if="loadError" class="btn btn--outline" @click="load">Retry</button>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { account } from '../auth-session.js'
import { useQuasar } from 'quasar'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import CodeViewer from '../components/CodeViewer.vue'
import EmptyState from '../components/EmptyState.vue'
import TriggerSnippet from '../components/TriggerSnippet.vue'
import WorkflowGraph from '../components/WorkflowGraph.vue'
import ExecutionFlow from '../components/ExecutionFlow.vue'
import WorkflowInputForm from '../components/WorkflowInputForm.vue'
import WorkflowRuns from '../components/WorkflowRuns.vue'
import { initialInputs, inputKey, runLocation } from '../workflow-inputs'
import { compareGraphs } from '../flow'
import { RUN_TONE, diffLines, fullTime, scheduleTime } from '../format'
import { backTo } from '../router'
import { actions, onLiveEvent, store } from '../store'
import { api } from '../api'

const $q = useQuasar()
const route = useRoute()
const router = useRouter()

const tabs = ['Overview', 'Runs', 'Definition', 'Settings']
const chatModes = [
  { value: 'same', label: 'One chat' },
  { value: 'new', label: 'A chat per run' }
]
const tab = computed({
  get: () => draft.value ? (route.query.tab === 'Code diff' ? 'Code diff' : 'Flow') :
    ({ Flow: 'Definition', Code: 'Definition', History: 'Runs', Run: 'Settings' }[route.query.tab] || (tabs.includes(route.query.tab) ? route.query.tab : 'Overview')),
  set: value => router.replace({ query: { ...route.query, tab: value === 'Overview' ? undefined : value } })
})
const definitionTab = computed({
  get: () => route.query.tab === 'Code' || route.query.source === 'Code' ? 'Code' : 'Flow',
  set: value => router.replace({ query: { ...route.query, tab: 'Definition', source: value } })
})
const runOpen = computed({
  get: () => route.query.run === 'new',
  set: value => router.replace({ query: { ...route.query, run: value ? 'new' : undefined } })
})
const selectedRun = ref('')
const draftMode = ref('changes')
const loadError = ref('')
let loadVersion = 0
const workflow = ref(null)
const draft = ref(null)
const validation = ref(null)
const inputs = ref({})
const scheduleInputs = ref({})
const manualForm = ref(null), scheduleForm = ref(null), runTrigger = ref(null)
const runErrors = ref({}), scheduleErrors = ref({}), runError = ref('')
const initialManual = ref('{}')
const manualDirty = computed(() => JSON.stringify(inputs.value) !== initialManual.value)
const running = ref(false)
const scheduling = ref(false)
const approving = ref(false)
const togglingDisabled = ref(false)
const workflowControlError = ref('')
const scheduleFrequency = ref('daily')
const scheduleAt = ref('')
const scheduleMinute = ref(0)
const scheduleDays = ref([])
const scheduleMonthDay = ref(new Date().getDate())
const scheduleTimezone = ref(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
const weekDays = [
  { value: 'monday', label: 'Mon' },
  { value: 'tuesday', label: 'Tue' },
  { value: 'wednesday', label: 'Wed' },
  { value: 'thursday', label: 'Thu' },
  { value: 'friday', label: 'Fri' },
  { value: 'saturday', label: 'Sat' },
  { value: 'sunday', label: 'Sun' }
]
const timezones = [...new Set([
  scheduleTimezone.value,
  'UTC',
  ...(typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [])
])]

const name = computed(() => route.params.name || '')
const inputSchema = computed(() => workflow.value?.manifest?.inputs || {})
const hasResourceInputs = computed(() => Object.values(inputSchema.value.properties || {}).some(field => field.x_resource))
const latestRun = computed(() => workflow.value?.runs?.[0])
const latestSummary = computed(() => {
  const result = latestRun.value?.result
  return typeof result?.summary === 'string' ? result.summary : typeof result === 'string' ? result : ''
})
const disabled = computed(() => Boolean(workflow.value?.settings?.disabled))
const chatMode = computed(() => workflow.value?.settings?.chat_mode || 'same')
const scheduleReady = computed(() => {
  if (!scheduleTimezone.value || hasResourceInputs.value || workflow.value?.schedule_status === 'unavailable') return false
  if (scheduleFrequency.value === 'custom') return false
  if (scheduleFrequency.value === 'hourly') {
    return scheduleMinute.value >= 0 && scheduleMinute.value <= 59
  }
  if (!scheduleAt.value) return false
  if (scheduleFrequency.value === 'weekly') return scheduleDays.value.length > 0
  if (scheduleFrequency.value === 'monthly') {
    return scheduleMonthDay.value >= 1 && scheduleMonthDay.value <= 31
  }
  return true
})
const draftGraph = computed(() => {
  if (!draft.value) return null
  if (draftMode.value === 'proposed') return draft.value.graph
  if (draftMode.value === 'deployed') return draft.value.previous_graph
  return compareGraphs(draft.value.previous_graph, draft.value.graph)
})

const files = computed(() => [
  { name: `${workflow.value.name}.py`, code: workflow.value.code, language: 'python', icon: 'description' },
  {
    name: 'manifest.json',
    code: JSON.stringify(workflow.value.manifest, null, 2),
    language: 'json',
    icon: 'data_object'
  }
])

async function load () {
  const version = ++loadVersion
  workflow.value = null
  draft.value = null
  validation.value = null
  loadError.value = ''
  workflowControlError.value = ''
  selectedRun.value = ''
  draftMode.value = 'changes'
  if (!name.value) return
  try {
    if (store.drafts.some((item) => item.name === name.value)) {
      const result = await api.draft(name.value)
      if (version !== loadVersion) return
      draft.value = result
      const report = result.validation || await api.validate(name.value, result.code)
      if (version === loadVersion) validation.value = report
      return
    }
    const result = await api.workflow(name.value)
    if (version !== loadVersion) return
    workflow.value = result
    const previous = store.workflowInputDraft
    const reuse = previous?.name === result.name && previous.account === account.user?.id
    inputs.value = initialInputs(result.manifest?.inputs || {}, reuse ? previous.input : {})
    if (reuse) store.workflowInputDraft = null
    initialManual.value = JSON.stringify(inputs.value)
    scheduleInputs.value = initialInputs(result.manifest?.inputs || {}, result.schedule?.input || {})
    runErrors.value = {}; scheduleErrors.value = {}; runError.value = reuse ? 'Review these previous inputs against the current workflow before starting a new execution.' : ''
    loadSchedule(result.schedule)
  } catch (error) {
    if (version === loadVersion) loadError.value = error.message
  }
}

function openRun () { runErrors.value = {}; runError.value = ''; runOpen.value = true }
function copyScheduledInputs () { inputs.value = initialInputs(inputSchema.value, workflow.value.schedule.input); runErrors.value = {} }
function confirmDiscard () {
  return new Promise(resolve => $q.dialog({ title: 'Discard run inputs?', message: 'Your unsaved manual inputs will be discarded.', cancel: true })
    .onOk(() => resolve(true)).onCancel(() => resolve(false)).onDismiss(() => resolve(false)))
}
async function closeRun () {
  if (running.value || (manualDirty.value && !await confirmDiscard())) return
  inputs.value = initialInputs(inputSchema.value); initialManual.value = JSON.stringify(inputs.value)
  runOpen.value = false
  await nextTick(); runTrigger.value?.focus()
}
function fieldErrors (error) { return Object.fromEntries((error.detail?.field_errors || []).map(item => [inputKey(item.path), item.message])) }
async function run () {
  runErrors.value = {}; runError.value = ''
  if (!manualForm.value?.validate()) { runError.value = 'Review the highlighted inputs before starting.'; return }
  running.value = true
  const version = loadVersion, target = workflow.value
  try {
    const started = await api.runWorkflow(target.name, inputs.value)
    if (version !== loadVersion) return
    initialManual.value = JSON.stringify(inputs.value)
    router.push(runLocation(target.name, started.workflow_id))
  } catch (error) {
    runErrors.value = fieldErrors(error)
    runError.value = error.detail?.input?.join(' · ') || error.message
  } finally { running.value = false }
}

async function schedule () {
  scheduleErrors.value = {}
  if (!scheduleForm.value?.validate()) { $q.notify({ type: 'negative', message: 'Review the scheduled inputs.' }); return }
  scheduling.value = true
  try {
    const definition = {
      frequency: scheduleFrequency.value,
      timezone: scheduleTimezone.value,
      input: scheduleInputs.value
    }
    if (scheduleFrequency.value === 'hourly') definition.minute = scheduleMinute.value
    else definition.at = scheduleAt.value
    if (scheduleFrequency.value === 'weekly') definition.days = scheduleDays.value
    if (scheduleFrequency.value === 'monthly') definition.day = scheduleMonthDay.value
    const saved = await api.schedule(workflow.value.name, definition)
    workflow.value.schedule = saved
    loadSchedule(saved)
    await actions.loadWorkflows()
  } catch (error) {
    scheduleErrors.value = fieldErrors(error)
    $q.notify({ type: 'negative', message: error.detail?.input?.join(' · ') || error.message })
  } finally { scheduling.value = false }
}

async function refreshSchedule () {
  const target = workflow.value
  try { const result = await api.workflow(target.name); if (target === workflow.value) { target.schedule = result.schedule; target.schedule_status = result.schedule_status } }
  catch (error) { $q.notify({ type: 'negative', message: error.message }) }
}

async function unschedule () {
  scheduling.value = true
  try {
    await api.unschedule(workflow.value.name)
    workflow.value.schedule = null
    await actions.loadWorkflows()
  } catch (error) {
    $q.notify({ type: 'negative', message: error.message })
  } finally {
    scheduling.value = false
  }
}

function loadSchedule (definition) {
  const supported = ['hourly', 'daily', 'weekly', 'monthly']
  scheduleFrequency.value = definition && !supported.includes(definition.frequency)
    ? 'custom'
    : definition?.frequency || 'daily'
  scheduleAt.value = definition?.at || ''
  scheduleMinute.value = definition?.minute ?? 0
  scheduleDays.value = [...(definition?.days || [])]
  scheduleMonthDay.value = definition?.day || new Date().getDate()
  scheduleTimezone.value = definition?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

function beginScheduleReplacement () {
  scheduleFrequency.value = 'daily'
  scheduleAt.value = ''
  scheduleDays.value = []
  scheduleMonthDay.value = new Date().getDate()
  scheduleTimezone.value = workflow.value.schedule?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

function toggleScheduleDay (day) {
  scheduleDays.value = scheduleDays.value.includes(day)
    ? scheduleDays.value.filter((value) => value !== day)
    : [...scheduleDays.value, day]
}

async function syncDisabled (value = disabled.value) {
  if (togglingDisabled.value) return
  const target = workflow.value
  togglingDisabled.value = true
  workflowControlError.value = ''
  try {
    target.settings = await api.workflowSettings(target.name, { disabled: value })
  } catch (error) {
    // A failed Temporal update may still have durably saved the control intent.
    if (error.detail?.settings) target.settings = error.detail.settings
    if (workflow.value === target) workflowControlError.value = error.message
  } finally {
    togglingDisabled.value = false
    actions.loadWorkflows().catch(() => {})
  }
}

function toggleDisabled () {
  return syncDisabled(!disabled.value)
}

async function setChatMode (mode) {
  workflow.value.settings = await api.workflowSettings(workflow.value.name, { chat_mode: mode })
}

function remove () {
  $q.dialog({ title: 'Delete workflow', message: `Delete ${workflow.value.name}?`, cancel: true })
    .onOk(async () => {
      await api.deleteWorkflow(workflow.value.name)
      await actions.loadWorkflows()
      router.push('/workflows')
    })
}

async function approve () {
  approving.value = true
  try {
    await api.approveDraft(draft.value.name)
    await actions.loadWorkflows()
    load()
  } catch (error) {
    $q.notify({ type: 'negative', message: error.message })
  } finally {
    approving.value = false
  }
}

async function discard () {
  await api.discardDraft(draft.value.name)
  await actions.loadWorkflows()
  router.push('/workflows')
}

onBeforeRouteLeave(async () => !runOpen.value || !manualDirty.value || await confirmDiscard())
onBeforeRouteUpdate(async to => !runOpen.value || !manualDirty.value || (to.params.name === name.value && to.query.run === 'new') || await confirmDiscard())
watch(inputs, () => { runErrors.value = {}; runError.value = '' })
watch(scheduleInputs, () => { scheduleErrors.value = {} })
watch(name, load)
watch(() => store.drafts.some((item) => item.name === name.value), load)
watch(() => store.workflows.find(item => item.name === name.value)?.settings, settings => { if (settings && workflow.value) workflow.value.settings = settings })
let off = () => {}, refreshVersion = 0
onMounted(() => {
  load()
  off = onLiveEvent(async event => {
    if (!event.kind?.startsWith('run.') || (event.workflow && event.workflow !== name.value) || !workflow.value) return
    const target = workflow.value, version = ++refreshVersion
    try { const result = await api.workflow(target.name); if (workflow.value === target && version === refreshVersion) target.runs = result.runs }
    catch { /* The run list keeps its independent retry/error state. */ }
  })
})
onUnmounted(() => { loadVersion++; refreshVersion++; off() })
</script>

<style scoped>
.overview-intro { border: 0; padding-bottom: 0; }
.overview-intro h2 { font-size: 1.25rem; margin: 0 0 12px; }
.overview-schedule { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 22px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); }
.latest-run { padding: 22px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-panel); }
.latest-summary { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.875rem; line-height: 1.6; }
.definition-tabs { background: var(--surface-panel); }
.process-pane > .tabs { min-width: 0; overflow-x: auto; }
@media (max-width: 560px) { .process-pane > .tabs { padding-inline: 4px; } .process-pane > .tabs .tab { padding-inline: 8px; font-size: 0.75rem; min-height: 44px; } }
.scheduled-inputs { padding-top: 24px; margin-top: 24px; border-top: 1px solid var(--border); }
.run-dialog { display: flex; flex-direction: column; width: min(820px, calc(100vw - 32px)); max-width: 820px; max-height: 90dvh; background: var(--surface-panel); color: var(--text); border-radius: var(--radius-lg); }
.run-dialog__head { display: flex; justify-content: space-between; gap: 16px; padding: 24px; border-bottom: 1px solid var(--border); }
.run-dialog__head h2 { margin: 0 0 8px; font-size: 1.25rem; }
.run-dialog__head p { margin: 0; font-size: 0.8125rem; color: var(--text-muted); }
.run-dialog__body { padding: 24px; min-height: 0; }
.run-dialog__footer { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 18px 24px; border-top: 1px solid var(--border); }
@media (max-width: 560px) { .run-action-label { display: none; } .run-action { min-width: 44px; min-height: 44px; padding: 8px; } .pane-head .avatar-sq { display: none; } .overview-schedule { flex-wrap: wrap; } .run-dialog { max-height: calc(100dvh - 24px); } .run-dialog__head, .run-dialog__body { padding: 16px; } .run-dialog__footer { padding: 14px; } }
.flow-picker {
  width: 100%;
  max-width: 290px;
  min-width: 0;
  height: 36px;
  padding: 6px 10px;
  font-size: 0.8125rem;
  text-overflow: ellipsis;
}

.draft-head {
  flex-wrap: wrap;
}

.draft-head > .grow {
  min-width: 100px;
}

.process-pane { min-width: 0; min-height: 0; }

.pane-body {
  padding: 28px 32px 48px;
}

.pane-body .section-label { margin: 0 0 8px; color: var(--text); font-size: 0.9375rem; font-weight: 600; text-transform: none; letter-spacing: -0.015em; }
.block__intro { max-width: 65ch; margin: 0 0 18px; color: var(--text-muted); font-size: 0.875rem; line-height: 1.7; }
.history-empty { padding: 28px 20px; margin-top: 20px; border: 1px dashed var(--border-strong); border-radius: var(--radius-md); text-align: center; }
.history-empty > .material-icons { color: var(--text-muted); font-size: 1.75rem; }
.history-empty h3 { margin: 12px 0 6px; font-size: 1rem; }
.history-empty p { margin: 0 0 18px; color: var(--text-muted); font-size: 0.875rem; line-height: 1.6; }

.block {
  max-width: 760px;
  margin-bottom: 32px;
  padding-bottom: 28px;
  border-bottom: 1px solid var(--border);
}

.block--wide {
  max-width: 1100px;
}

.notice {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 760px;
  margin-bottom: 20px;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 0.8125rem;
}

.notice .material-icons {
  font-size: 1.125rem;
}

.segmented {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
}

.segment {
  min-height: 36px;
  padding: 7px 12px;
  border: none;
  border-radius: var(--radius-xs);
  background: none;
  color: var(--text-muted);
  font: inherit;
  font-size: 0.78125rem;
  cursor: pointer;
}

@media (hover: hover) and (pointer: fine) {
  .segment:hover { color: var(--text); }
  .run-row:hover { background: var(--surface-hover); }
}
.segment:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.segment:active { background: var(--surface-hover); }

.segment--active {
  background: var(--accent-soft);
  color: var(--accent-hover);
}

.field-row {
  margin-top: 10px;
}

.field-row__label {
  display: block;
  margin-bottom: 5px;
  font-size: 0.78125rem;
  font-weight: 500;
  color: var(--text-muted);
}

.schedule-block {
  padding-top: 2px;
}

.schedule-current {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(180px, auto);
  align-items: center;
  gap: 12px;
  margin-top: 10px;
  padding: 16px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warning);
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
}

.schedule-current__icon {
  color: var(--warning);
  font-size: 1.25rem;
}

.schedule-current__rule,
.schedule-current__next {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.schedule-current__next {
  text-align: right;
}

.schedule-grid {
  display: grid;
  grid-template-columns: minmax(140px, 0.8fr) minmax(120px, 0.6fr) minmax(180px, 1.4fr);
  gap: 10px;
  margin-top: 14px;
}

.schedule-field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 5px;
  color: var(--text-muted);
  font-size: 0.78125rem;
  font-weight: 500;
}

.schedule-field--timezone {
  grid-column: -2 / -1;
}

.schedule-days {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}

.schedule-days__label {
  color: var(--text-muted);
  font-size: 0.78125rem;
  font-weight: 500;
}

.schedule-day {
  width: 42px;
  padding-inline: 0;
}

.schedule-actions {
  margin-top: 18px;
  flex-wrap: wrap;
}

.run-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 10px;
  border-bottom: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: inherit;
  text-decoration: none;
}

.avatar-sq {
  display: grid;
  place-items: center;
  flex: none;
  width: 38px;
  height: 38px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--warning-soft);
  color: var(--warning);
}

.avatar-sq .material-icons {
  font-size: 1.125rem;
}

.pane-head__back {
  display: none;
}

@media (max-width: 900px) {
  .pane-head__back {
    display: grid;
  }

  .pane-body {
    padding: 16px max(14px, env(safe-area-inset-right)) max(32px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
  }

  .pane-body .row {
    flex-wrap: wrap;
  }

  .pane-body .field {
    min-width: 0;
  }

  .schedule-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .schedule-field--timezone {
    grid-column: auto;
  }
}

@media (max-width: 560px) {
  .pane-head .chip {
    max-width: 92px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pane-head .btn--danger {
    padding: 0 8px;
  }

  .draft-head .btn--primary {
    flex: 1 1 100%;
    min-height: 40px;
  }

  .run-row {
    flex-wrap: wrap;
  }

  .run-row .mono {
    flex-basis: calc(100% - 90px);
  }

  .schedule-current {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .schedule-current__next {
    grid-column: 2;
    text-align: left;
  }

  .schedule-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .schedule-days {
    align-items: flex-start;
    flex-direction: column;
  }

  .schedule-days .segmented {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(7, minmax(0, 1fr));
  }

  .schedule-day {
    width: auto;
  }
}
@media (pointer: coarse) {
  .flow-picker, .pane-body .field { font-size: 1rem; }
  .segment { min-height: 44px; }
}
</style>
