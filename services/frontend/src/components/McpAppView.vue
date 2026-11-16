<template>
  <section ref="root" class="mcp-app" :aria-label="title" :style="document ? { minHeight: `${height}px` } : {}">
    <div v-if="!visible" class="mcp-app__notice caption dim">Interactive result loads when visible.</div>
    <p v-else-if="loading" class="mcp-app__notice caption" role="status">Loading {{ title }}…</p>
    <div v-if="failure" class="mcp-app__notice">
      <p class="caption" role="status">{{ failure }}</p>
      <button class="btn btn--sm btn--outline" @click="restart">Reload interface</button>
    </div>
    <iframe v-if="document && visible && !failure" ref="frame" class="mcp-app__frame" :title="title" :style="{ height: `${height}px` }" sandbox="allow-scripts allow-same-origin" referrerpolicy="no-referrer" />
    <p v-if="pendingLink" class="mcp-app__notice caption"><a :href="pendingLink" target="_blank" rel="noopener noreferrer" @click="pendingLink = ''">Open {{ linkHost }} in a new tab</a></p>
    <details v-if="fallback" class="mcp-app__fallback"><summary class="caption">Original result</summary><pre>{{ fallback }}</pre></details>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { request } from '../api'
import { connectApp } from '../mcp-apps'
import { currentTokens } from '../preferences'

const props = defineProps({
  app: { type: Object, default: null },
  instance: { type: Object, default: null },
  chatId: { type: String, default: '' },
  sandboxUrl: { type: String, default: '' }
})
const root = ref(null), frame = ref(null), document = ref(null), visible = ref(false)
const loading = ref(false), failure = ref(''), height = ref(320), pendingLink = ref('')
const title = computed(() => props.app?.title || props.instance?.title || 'Interactive result')
const fallback = computed(() => {
  const result = document.value?.result
  if (!result) return ''
  const text = result.content?.filter(p => p.type === 'text').map(p => p.text).join('\n\n') || ''
  return [text, result.structuredContent ? JSON.stringify(result.structuredContent, null, 2) : ''].filter(Boolean).join('\n\n')
})
const linkHost = computed(() => { try { return new URL(pendingLink.value).hostname } catch { return '' } })
const hostContext = () => ({
  theme: window.getComputedStyle(window.document.documentElement).colorScheme === 'dark' ? 'dark' : 'light',
  displayMode: 'inline', availableDisplayModes: ['inline'], platform: 'web',
  locale: navigator.language, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  containerDimensions: { maxHeight: 1400 },
  styles: { variables: {
    '--color-background-primary': currentTokens.value['surface-app'],
    '--color-background-secondary': currentTokens.value['surface-panel'],
    '--color-text-primary': currentTokens.value.text,
    '--color-text-secondary': currentTokens.value['text-muted'],
    '--color-border-primary': currentTokens.value.border,
    '--color-text-info': currentTokens.value.accent
  } }
})
let bridge, observer, controller, generation = 0, destroyed = false
async function close () { const previous = bridge; bridge = null; await previous?.close() }
async function load () {
  const mine = ++generation
  controller?.abort()
  controller = new AbortController()
  await close()
  if (!visible.value || destroyed) return
  loading.value = true; failure.value = ''; pendingLink.value = ''
  try {
    const signal = controller.signal
    const data = props.app || await request(`/api/chats/${encodeURIComponent(props.chatId)}/apps/${encodeURIComponent(props.instance.id)}`, { signal })
    if (mine !== generation || destroyed) return
    document.value = data
    const config = props.sandboxUrl ? { sandbox_url: props.sandboxUrl } : await request('/api/mcp-apps/runtime', { signal })
    if (mine !== generation || destroyed) return
    if (!config.sandbox_url) throw new Error('Interactive apps need a configured sandbox origin. See Settings → Apps. The original result is unchanged.')
    await nextTick()
    if (mine !== generation || !frame.value || destroyed) return
    const connection = await connectApp(frame.value, data, config.sandbox_url, {
      context: hostContext(),
      resize: value => { if (mine === generation) height.value = value },
      ready: () => { if (mine === generation) loading.value = false },
      error: text => { if (mine === generation) { loading.value = false; failure.value = text } },
      openLink: url => {
        if (mine !== generation) return
        if (!navigator.userActivation?.isActive || !window.open(url, '_blank', 'noopener,noreferrer')) pendingLink.value = url
      }
    })
    if (mine !== generation || destroyed) await connection.close()
    else bridge = connection
  } catch (error) {
    if (mine !== generation || error.name === 'AbortError' || destroyed) return
    loading.value = false; failure.value = error.message || 'The interface could not load. The original result is unchanged.'
  }
}
function restart () { document.value = null; load() }
// Compare stable identities individually: live chat snapshots replace descriptor
// objects without changing the saved app instance or its mounted local state.
watch([() => props.app, () => props.instance?.id, () => props.chatId, () => props.sandboxUrl], load)
watch(currentTokens, () => bridge?.setContext(hostContext()), { deep: true })
onMounted(() => {
  if (!window.IntersectionObserver) { visible.value = true; load(); return }
  observer = new IntersectionObserver(entries => {
    if (visible.value || destroyed || !entries.some(entry => entry.isIntersecting)) return
    // Lazy-start once, not once per viewport entry. Keep the iframe and its
    // local state alive while this message remains mounted in the chat.
    visible.value = true
    observer.disconnect()
    load()
  }, { rootMargin: '600px' })
  observer.observe(root.value)
})
onBeforeUnmount(() => { destroyed = true; ++generation; observer?.disconnect(); controller?.abort(); close() })
</script>

<style scoped>
.mcp-app { min-width: 0; margin: 10px 0; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; background: var(--surface-app); }
.mcp-app__frame { display: block; border: 0; width: 100%; max-height: 1400px; }
.mcp-app__notice { padding: 10px 14px; margin: 0; }
.mcp-app__notice p { margin: 0 0 8px; }
.mcp-app__notice a { color: var(--accent); overflow-wrap: anywhere; }
.mcp-app__fallback { border-top: 1px solid var(--border); padding: 8px 14px; }
.mcp-app__fallback summary { cursor: pointer; color: var(--text-muted); }
.mcp-app__fallback pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; max-height: 420px; overflow: auto; }
</style>
