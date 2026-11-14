<template>
  <section id="account-sharing" class="sharing">
    <h3>Shared resources</h3>
    <p class="muted">Conversations and their files stay private—even from other administrators. Share saved workspace resources deliberately.</p>
    <p v-if="error" class="account-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="account-notice" role="status">{{ notice }}</p>
    <form class="account-form" @submit.prevent="share">
      <label>Person<select v-model="userId" class="field" required :disabled="busy"><option value="" disabled>Choose a member</option><option v-for="user in members" :key="user.id" :value="user.id">{{ user.display_name || user.username }} · @{{ user.username }}</option></select></label>
      <label>Resource type<select v-model="kind" class="field" :disabled="busy"><option value="agent">Agent</option><option value="project">Project & repository</option><option value="workflow">Workflow</option><option value="app">Miniapp</option><option value="tool">Individual tool</option></select></label>
      <label>Resource<select v-model="resourceId" class="field" required :disabled="busy"><option value="" disabled>Choose a resource</option><option v-for="resource in choices" :key="resource.resource_id" :value="resource.resource_id">{{ resource.name || resource.resource_id }}</option></select></label>
      <p class="caption muted">Share an agent first, then its required projects and individual tools. Member agents are conversation/shared-tool sessions: local shell, filesystem, native applications, extensions and infrastructure administration remain administrator-only. Shared workflows/apps run their administrator-reviewed server code. Review a tool’s configured external identity before sharing it.</p>
      <label v-if="['agent', 'workflow', 'app'].includes(kind)" class="sharing-check"><input v-model="trusted" type="checkbox" required :disabled="busy" /> I trust this person to run this saved resource in our shared infrastructure.</label>
      <button class="btn btn--primary" :disabled="busy || !members.length">Share resource</button>
      <p v-if="!members.length" class="caption muted">Invite and activate a member account before sharing resources. Administrators already manage workspace resources.</p>
    </form>
    <ul class="sharing-list"><li v-for="grant in grants" :key="`${grant.kind}:${grant.resource_id}:${grant.user_id}`"><div class="grow"><strong>{{ resourceName(grant) }}</strong><p class="caption muted">{{ grant.kind }} · Shared with {{ personName(grant.user_id) }}</p></div><button class="btn" :disabled="busy" @click="remove(grant)">Remove access</button></li></ul>
    <p v-if="!grants.length && !busy" class="caption muted">Nothing is shared with members yet.</p>
  </section>
</template>
<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { accountApi } from '../../account.js'
import { api } from '../../api.js'
const resources = ref([]), users = ref([]), grants = ref([]), tools = ref([])
const userId = ref(''), kind = ref('agent'), resourceId = ref(''), trusted = ref(false)
const busy = ref(false), error = ref(''), notice = ref('')
const members = computed(() => users.value.filter(user => user.role === 'member' && user.status === 'active'))
const choices = computed(() => kind.value === 'tool' ? tools.value.filter(tool => !tool.name.startsWith('workflows_')).map(tool => ({ resource_id: tool.name, name: tool.name })) : resources.value.filter(resource => resource.kind === kind.value))
watch([kind, userId], () => { resourceId.value = ''; trusted.value = false })
watch(resourceId, () => { trusted.value = false })
const personName = id => users.value.find(user => user.id === id)?.username || id
const resourceName = grant => resources.value.find(resource => resource.kind === grant.kind && resource.resource_id === grant.resource_id)?.name || grant.resource_id
async function load () {
  const [inventory, people, shared, catalog] = await Promise.all([accountApi.resources(), accountApi.users(), accountApi.grants(), api.catalog()])
  resources.value = inventory.resources || []; users.value = people.users || []; grants.value = shared.grants || []; tools.value = catalog.tools || []
}
async function perform (task, success) { busy.value = true; error.value = notice.value = ''; try { await task(); await load(); notice.value = success } catch (e) { error.value = e.message } finally { busy.value = false } }
function share () { return perform(async () => { await accountApi.share({ kind: kind.value, resource_id: resourceId.value, user_id: userId.value }); trusted.value = false }, 'Resource shared. Private conversations remain private.') }
function remove (grant) { return perform(() => accountApi.unshare({ kind: grant.kind, resource_id: grant.resource_id, user_id: grant.user_id }), 'Access removed. Active execution rechecks permissions.') }
onMounted(async () => { busy.value = true; try { await load() } catch (e) { error.value = e.message } finally { busy.value = false } })
</script>
<style scoped>
.sharing { border-top: 1px solid var(--border); margin-top: 2rem; padding-top: 1.5rem; }
.sharing h3 { font-size: 1rem; font-weight: 600; }
.sharing .account-form { max-width: 29rem; }
.sharing-check { display: flex; align-items: flex-start; line-height: 1.6; }
.sharing-check input { flex: none; width: 1.1rem; min-height: 1.1rem; margin-top: .2rem; }
.sharing-list { margin: 1.5rem 0; padding: 0; list-style: none; }
.sharing-list li { display: flex; align-items: center; flex-wrap: wrap; gap: .75rem; border-top: 1px solid var(--border); padding: 1rem 0; overflow-wrap: anywhere; }
.sharing .btn { min-height: 44px; }
</style>
