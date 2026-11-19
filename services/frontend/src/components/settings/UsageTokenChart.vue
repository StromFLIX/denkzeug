<template>
  <div class="usage-visuals" :aria-busy="loading">
    <section class="range" aria-label="Selected range token breakdown">
      <header class="range__head">
        <div><h3>Selected range</h3><span v-if="stale" class="range__status" role="status">{{ loading ? 'Updating grouping…' : 'Showing previous grouping' }}</span></div>
        <label class="range__group">Group by <select :value="grouping" class="field" @change="$emit('group', $event.target.value)"><option v-for="[key, label] in groups" :key="key" :value="key">{{ label }}</option></select></label>
      </header>
      <template v-if="series.length">
        <div class="range__stack" aria-hidden="true"><span v-for="(item, i) in series" :key="item.id" :style="{ width: `${item.total / total * 100}%`, '--series': colors[i] }" /></div>
        <dl class="range__legend" :style="{ '--columns': series.length }">
          <div v-for="(item, i) in series" :key="item.id">
            <dt :title="item.label"><i :style="{ '--series': colors[i] }" /> <span>{{ item.label }}<template v-if="item.count"> ({{ item.count }})</template></span></dt>
            <dd><strong :title="exact(item.total)">{{ compactTokens(item.total) }}</strong><span>{{ share(item.total, total) }}</span></dd>
          </div>
        </dl>
      </template>
    </section>
    <section class="chart" aria-label="Token consumption over time">
      <header class="chart__head">
        <h3>Daily reported tokens</h3>
        <span v-if="series.length && !longRange" class="chart__note">Same groups and colors as the selected range</span>
        <select v-if="series.length" ref="picker" class="field chart__picker" :class="{ 'chart__picker--long': longRange }" :value="selectedDate" aria-label="Select a day to inspect" @change="selectDate($event.target.value, true)">
          <option value="">Select day</option><option v-for="day in days" :key="day.date" :value="day.date">{{ dateLabel(day.date, longRange) }}</option>
        </select>
      </header>
      <p v-if="!summary.executions" class="chart__empty">No agent executions in this period. Try a wider date range.</p>
      <p v-else-if="!summary.tokens" class="chart__empty">These executions did not report token counts.</p>
      <template v-else-if="series.length">
        <div class="chart__plot">
          <div class="chart__axis" aria-hidden="true"><span v-for="(tick, i) in ticks" :key="i" :style="{ top: `${i * 25}%` }">{{ compactTokens(tick) }}</span></div>
          <div ref="plot" class="chart__scroll">
            <div class="chart__track" :style="{ '--min-track': longRange ? `${8 * days.length}px` : '0px' }">
              <div class="chart__bars" :class="{ 'chart__bars--dense': days.length > 45 }" role="list" aria-label="Tokens by date">
                <div v-for="(day, i) in days" :key="day.date" class="chart__column" role="listitem">
                  <button type="button" class="chart__bar" :data-date="day.date" :aria-label="bucketLabel(day)" :aria-pressed="selectedDate === day.date" :tabindex="(focusedDate || selectedDate || days[0]?.date) === day.date ? 0 : -1" @click="selectDate(selectedDate === day.date ? '' : day.date)" @focus="focusedDate = day.date" @keydown="barKeys($event, i)">
                    <span v-for="item in daySeries(day)" :key="item.id" aria-hidden="true" :style="{ height: `${item.tokens / ceiling * 100}%`, '--series': item.color }" />
                  </button>
                </div>
              </div>
              <div class="chart__dates" aria-hidden="true">
                <span v-for="tick in dateTicks" :key="tick.date" :class="{ 'chart__date--middle': tick.i > 0 && tick.i < days.length - 1, 'chart__date--end': tick.i > 0 && tick.i === days.length - 1 }" :style="{ left: `${tick.i / Math.max(1, days.length - 1) * 100}%` }">{{ dateLabel(tick.date) }}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="chart__detail">
          <div class="chart__detail-content" role="status">
            <template v-if="selected">
              <div class="chart__detail-heading"><strong>{{ dateLabel(selected.date, longRange) }}</strong> · {{ selected.tokens ? `${compactTokens(selected.tokens.total)} tokens` : (selected.executions ? 'No token counts reported' : 'No agent executions') }}</div>
              <ul v-if="selected.tokens" class="chart__detail-values">
                <li v-for="item in daySeries(selected)" :key="item.id"><i :style="{ '--series': item.color }" />{{ item.label }} <strong :title="exact(item.tokens)">{{ compactTokens(item.tokens) }}</strong></li>
              </ul>
            </template>
            <span v-else>{{ longRange ? 'Scroll to explore, or choose a day to see its breakdown.' : 'Select a day to see its breakdown.' }}</span>
          </div>
          <button v-if="selected" type="button" class="chart__clear" @click="clearDay">Clear day</button>
        </div>
      </template>
      <p v-else class="chart__empty">Grouped chart data is unavailable.</p>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { compactTokens, dateLabel, share } from '../../usage'

const props = defineProps({
  chart: Object,
  days: { type: Array, required: true },
  summary: { type: Object, required: true },
  groups: { type: Array, required: true },
  grouping: { type: String, required: true },
  loading: Boolean
})
defineEmits(['group'])
const colors = Array.from({ length: 5 }, (_, i) => `var(--series-${i + 1})`)
const picker = ref(null), plot = ref(null), selectedDate = ref(''), focusedDate = ref('')
const longRange = computed(() => props.days.length > 90)
const series = computed(() => props.chart?.series || [])
const stale = computed(() => props.chart && props.chart.group !== props.grouping)
const total = computed(() => props.summary.tokens?.total || 0)
const selected = computed(() => props.days.find(day => day.date === selectedDate.value))
const ceiling = computed(() => {
  const max = Math.max(4, ...props.days.map(day => day.tokens?.total || 0))
  const unit = 10 ** Math.floor(Math.log10(max))
  return Math.ceil(max / unit) * unit
})
const ticks = computed(() => [1, .75, .5, .25, 0].map(ratio => Math.round(ceiling.value * ratio)))
const dateTicks = computed(() => {
  const days = props.days
  if (!days.length) return []
  let indexes = [0]
  if (longRange.value) {
    days.forEach((day, i) => { if (i - indexes.at(-1) >= 10 && day.date.endsWith('-01')) indexes.push(i) })
    if (days.length - 1 - indexes.at(-1) >= 10) indexes.push(days.length - 1)
  } else {
    if (days.length > 2) indexes.push(Math.floor(days.length / 2))
    if (days.length > 1) indexes.push(days.length - 1)
  }
  return indexes.map(i => ({ date: days[i].date, i }))
})
const exact = n => n.toLocaleString()
const daySeries = day => series.value.map((item, i) => ({ ...item, tokens: day.series?.[i] || 0, color: colors[i] })).filter(item => item.tokens > 0)
function bucketLabel (day) {
  const date = dateLabel(day.date, true)
  if (!day.tokens) return `${date}: ${day.executions ? 'No token counts reported' : 'No agent executions'}`
  return `${date}: ${exact(day.tokens.total)} tokens; ${daySeries(day).map(item => `${item.label} ${exact(item.tokens)}`).join(', ')}`
}
function dayBar (date) { return plot.value?.querySelector(`[data-date="${date}"]`) }
function selectDate (date, reveal = false) {
  selectedDate.value = date
  if (date) focusedDate.value = date
  if (!reveal || !date || !plot.value) return
  const scroll = plot.value
  const bar = dayBar(date)
  if (bar && scroll.scrollWidth > scroll.clientWidth) {
    scroll.scrollLeft += bar.getBoundingClientRect().left - scroll.getBoundingClientRect().left - (scroll.clientWidth - bar.offsetWidth) / 2
  }
}
async function clearDay () {
  const bar = dayBar(selectedDate.value)
  selectDate('')
  await nextTick()
  const target = picker.value?.getClientRects().length ? picker.value : bar
  target?.focus({ preventScroll: true })
}
async function barKeys (event, i) {
  const last = props.days.length - 1
  const target = { ArrowLeft: Math.max(0, i - 1), ArrowRight: Math.min(last, i + 1), Home: 0, End: last }[event.key]
  if (target === undefined) return
  event.preventDefault()
  const date = props.days[target].date
  selectDate(date, true)
  await nextTick()
  dayBar(date)?.focus({ preventScroll: true })
}
watch(() => props.days, days => {
  if (!days.some(day => day.date === selectedDate.value)) selectedDate.value = ''
  if (!days.some(day => day.date === focusedDate.value)) focusedDate.value = ''
})
</script>

<style scoped>
.usage-visuals { --series-1: #257c72; --series-2: #5266b6; --series-3: #9a601f; --series-4: #924e91; --series-5: #62727e; }
:global([data-color-scheme='dark'] .usage-visuals) { --series-1: #79dec4; --series-2: #a6b5ff; --series-3: #efbe7b; --series-4: #d5a3db; --series-5: #a0afb4; }
.usage-visuals h3 { margin: 0; font-size: .8125rem; font-weight: 650; }
.usage-visuals i { width: .4rem; height: .4rem; flex: none; border-radius: 2px; background: var(--series); }
.range { border-top: 1px solid var(--border); padding-top: .8rem; margin-bottom: 1rem; }
.range__head, .range__group, .chart__head { display: flex; align-items: center; gap: .55rem; }
.range__head, .chart__head { justify-content: space-between; }
.range__group, .range__status, .chart__note { font-size: .625rem; color: var(--text-muted); }
.range__group { flex: none; white-space: nowrap; }
.range__group .field, .chart__picker.field { width: auto; min-height: 2rem; padding: .3rem .5rem; font-size: .6875rem; }
.range__group .field { min-width: 5.4rem; }
.range__stack { display: flex; height: .52rem; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: .55rem; }
.range__stack span { background: var(--series); }
.range__stack span + span { box-shadow: inset 1px 0 var(--surface-app); }
.range__legend { display: grid; grid-template-columns: repeat(var(--columns), minmax(0, 1fr)); gap: .45rem .6rem; margin: .55rem 0 0; }
.range__legend > div + div { border-inline-start: 1px solid var(--border); padding-inline-start: .55rem; }
.range__legend dt { display: flex; gap: .3rem; align-items: center; color: var(--text-muted); font-size: .625rem; }
.range__legend dt span { overflow-wrap: anywhere; min-width: 0; }
.range__legend dd { margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 .35rem; }
.range__legend strong { font-size: .8125rem; }
.range__legend dd span { color: var(--text-muted); font-size: .625rem; }
.chart { --plot-height: 8.6rem; background: var(--surface-panel); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: .8rem .9rem 0; }
.chart__note { text-align: right; }
.chart__picker { display: none; max-width: 48%; }
.chart__picker--long { display: block; }
.chart__plot { display: grid; grid-template-columns: 2.45rem minmax(0, 1fr); gap: .45rem; margin: .85rem 0 .45rem; }
.chart__axis { position: relative; height: var(--plot-height); color: var(--text-muted); font-size: .55rem; text-align: end; }
.chart__axis span { position: absolute; inset-inline-end: 0; transform: translateY(-50%); }
.chart__scroll { overflow-x: auto; padding-top: 2px; margin-top: -2px; }
.chart__track { min-width: var(--min-track); }
.chart__bars { display: flex; height: var(--plot-height); gap: clamp(1px, .34vw, 4px); border-top: 1px solid var(--border); background: repeating-linear-gradient(to top, var(--border) 0px, var(--border) 1px, transparent 1px, transparent 25%); }
.chart__bars--dense { gap: 1px; }
.chart__column { flex: 1; min-width: 0; }
.chart__bar { position: relative; display: flex; flex-direction: column-reverse; width: 100%; height: 100%; border: 0; padding: 0; background: transparent; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.chart__bar span { display: block; background: var(--series); width: 100%; flex: none; pointer-events: none; }
.chart__bar span:last-child { border-radius: 2px 2px 0 0; }
.chart__bar:hover, .chart__bar[aria-pressed='true'] { background: var(--accent-soft); }
.chart__bar[aria-pressed='true']::after { content: ''; pointer-events: none; position: absolute; inset: 0 0 auto; border-top: 2px solid var(--text); }
.chart__bar:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.chart__dates { position: relative; height: 1.15rem; color: var(--text-muted); font-size: .55rem; }
.chart__dates span { position: absolute; top: .25rem; white-space: nowrap; }
.chart__date--middle { transform: translateX(-50%); }
.chart__date--end { transform: translateX(-100%); }
.chart__detail { display: flex; align-items: center; justify-content: space-between; gap: .55rem; min-height: 2.3rem; border-top: 1px solid var(--border); padding: .4rem 0; color: var(--text-muted); font-size: .625rem; }
.chart__detail-content { display: flex; flex-wrap: wrap; align-items: center; gap: .25rem .55rem; min-width: 0; }
.chart__detail-content strong { color: var(--text); font-weight: 600; }
.chart__detail-values { display: flex; flex-wrap: wrap; gap: .2rem .55rem; padding: 0; margin: 0; list-style: none; }
.chart__detail-values li { display: flex; gap: .25rem; align-items: center; flex-wrap: wrap; overflow-wrap: anywhere; min-width: 0; }
.chart__clear { border: 0; background: transparent; color: var(--text-muted); font: inherit; white-space: nowrap; padding: .3rem 0 .3rem .2rem; cursor: pointer; }
.chart__clear:hover { color: var(--text); text-decoration: underline; }
.chart__empty { padding: .7rem 0 1rem; font-size: .75rem; color: var(--text-muted); }
@media (max-width: 700px) {
  .range__legend { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .range__legend > div + div { border: 0; padding: 0; }
  .chart { --plot-height: 7.5rem; padding-inline: .55rem; }
  .chart__picker { display: block; }
  .range__group .field, .chart__picker.field, .chart__clear { min-height: 44px; }
  .chart__note { display: none; }
  .chart__plot { grid-template-columns: 1.85rem minmax(0, 1fr); gap: .3rem; }
  .chart__bars:not(.chart__bars--dense) { gap: 2px; }
  .chart__detail { align-items: start; }
  .chart__detail-heading { flex-basis: 100%; }
}
</style>
