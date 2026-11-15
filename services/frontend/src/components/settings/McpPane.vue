<template>
  <h2 class="settings__title">MCP servers</h2>
  <p class="settings__intro">Connect external tools and services. Agents and chats choose which connected tools they can use.</p>
  <div id="mcp-servers" class="setting">
    <div class="row integration-head">
      <div class="setting__label grow">Servers</div>
      <button class="btn btn--sm btn--outline" :disabled="loading" @click="refresh">
        {{ loading ? 'Checking…' : 'Refresh' }}
      </button>
      <button class="btn btn--sm btn--primary" :disabled="!state.writable || adding" @click="openForm(null)">
        Add server
      </button>
    </div>
    <p class="caption dim">Connected tools are available in every chat.</p>
    <div v-if="store.mcpAuth.snoozedUntil" class="row integration-snooze" role="status">
      <p class="caption grow">Login reminders on this device are snoozed until {{ new Date(store.mcpAuth.snoozedUntil).toLocaleString() }}. Other devices are unaffected.</p>
      <button class="btn btn--sm btn--outline" @click="actions.resumeMcpLoginReminders()">Resume login reminders</button>
    </div>

    <div v-if="adding" class="integration-add">
      <strong>{{ editing ? `Connection settings · ${editing}` : 'Add a server' }}</strong>
      <DeclaredField
        v-for="field in basicFields"
        :key="field.key"
        v-model="draft[field.key]"
        :field="field"
        :disabled="Boolean(busy) || (Boolean(editing) && field.key === 'name')"
      />
      <details :open="advanced" class="integration-details" @toggle="advanced = $event.target.open">
        <summary>Advanced settings</summary>
        <label class="integration-field"
          ><span class="caption">Transport</span>
          <select v-model="transport" class="field" aria-label="Transport" :disabled="Boolean(busy)">
            <option value="http">HTTP — connect to a server</option>
            <option value="stdio">stdio — run inside agentgateway</option>
          </select>
        </label>
        <label class="integration-field"
          ><span class="caption">Authentication</span>
          <select
            v-model="authentication"
            class="field"
            aria-label="Authentication"
            :disabled="Boolean(busy)"
          >
            <option value="auto">Automatic</option>
            <option value="token">
              {{ transport === 'http' ? 'Access token / none' : 'Environment credentials / none' }}
            </option>
            <option value="oauth">{{ transport === 'http' ? 'OAuth' : 'CLI OAuth' }}</option>
          </select>
        </label>
        <template v-if="transport === 'http'">
          <DeclaredField
            v-if="authentication === 'token' && tokenField"
            v-model="draft.token"
            :field="tokenField"
            :credential="editingCredential"
            :disabled="Boolean(busy)"
          />
          <template v-else>
            <p class="caption dim">
              Provider settings are detected automatically. Only override these if the provider requires a
              custom client.
            </p>
            <label class="integration-field">
              <span class="caption">OAuth grant</span>
              <select v-model="httpConfig.grant_type" class="field" aria-label="OAuth grant" :disabled="Boolean(busy)">
                <option value="authorization_code">Authorization code — browser sign-in</option>
                <option value="device_code">Device code — enter a code in your browser</option>
                <option value="client_credentials">Client credentials — service account</option>
              </select>
            </label>
            <label v-if="httpConfig.grant_type === 'authorization_code'" class="integration-field">
              <span class="caption">Callback destination</span>
              <select v-model="httpConfig.callback_mode" class="field" aria-label="Callback destination" :disabled="Boolean(busy)">
                <option value="desktop">Desktop app — local callback</option>
                <option value="web">Web — backend callback</option>
              </select>
            </label>
            <label class="integration-field"
              ><span class="caption">OAuth client ID</span
              ><input
                v-model="httpConfig.client_id"
                class="field mono"
                aria-label="OAuth client ID"
                :disabled="Boolean(busy)"
                placeholder="Automatic"
            /></label>
            <label v-if="httpConfig.grant_type === 'authorization_code'" class="integration-field"
              ><span class="caption">Callback URI</span
              ><input
                v-model="httpConfig.redirect_uri"
                class="field mono"
                aria-label="Callback URI"
                :disabled="Boolean(busy) || httpConfig.callback_mode === 'web'"
                :placeholder="httpConfig.callback_mode === 'web' ? (capabilities.http?.web_callback || 'Configure the public HTTPS instance URL') : 'Provider default'"
            /></label>
            <label class="integration-field"
              ><span class="caption">OAuth scopes</span
              ><input
                v-model="httpConfig.scope"
                class="field mono"
                aria-label="OAuth scopes"
                :disabled="Boolean(busy)"
                placeholder="Provider default"
            /></label>
            <label v-if="httpConfig.grant_type === 'authorization_code'" class="integration-field"
              ><span class="caption">Callback response mode</span
              ><select
                v-model="httpConfig.response_mode"
                class="field"
                aria-label="Callback response mode"
                :disabled="Boolean(busy)"
              >
                <option value="">Automatic</option>
                <option value="query">GET redirect</option>
                <option value="form_post">Form POST</option>
              </select></label
            >
            <label class="integration-field">
              <span class="caption">Token endpoint authentication</span>
              <select v-model="httpConfig.token_endpoint_auth_method" class="field" aria-label="Token endpoint authentication" :disabled="Boolean(busy)">
                <option value="none">None — public client</option>
                <option value="client_secret_basic">Client secret — HTTP Basic</option>
                <option value="client_secret_post">Client secret — request body</option>
                <option value="private_key_jwt">Private key JWT</option>
                <option value="client_secret_jwt">Client secret JWT</option>
              </select>
            </label>
            <label v-if="httpConfig.token_endpoint_auth_method.startsWith('client_secret')" class="integration-field">
              <span class="caption">Client secret</span>
              <input v-model="httpConfig.client_secret" type="password" class="field" aria-label="Client secret" autocomplete="new-password" :disabled="Boolean(busy)" :placeholder="storedOAuth.client_secret_configured ? 'Stored — leave blank to keep' : 'Client secret'" />
            </label>
            <template v-if="httpConfig.token_endpoint_auth_method === 'private_key_jwt'">
              <label class="integration-field">
                <span class="caption">Private key (PEM, write-only)</span>
                <textarea v-model="httpConfig.private_key" class="field mono" aria-label="Private key (PEM)" rows="3" autocomplete="off" spellcheck="false" :disabled="Boolean(busy)" :placeholder="storedOAuth.private_key_configured ? 'Stored — leave blank to keep' : 'Paste PEM private key'" />
              </label>
            </template>
            <label v-if="httpConfig.token_endpoint_auth_method.endsWith('_jwt')" class="integration-field">
              <span class="caption">Key ID (optional)</span>
              <input v-model="httpConfig.key_id" class="field mono" aria-label="Key ID" :disabled="Boolean(busy)" />
            </label>
            <label v-if="httpConfig.token_endpoint_auth_method.endsWith('_jwt')" class="integration-field">
              <span class="caption">JWT signing algorithm</span>
              <input v-model="httpConfig.token_endpoint_auth_signing_alg" class="field mono" aria-label="JWT signing algorithm" placeholder="Automatic — match provider and key" :disabled="Boolean(busy)" />
            </label>
            <label class="integration-field">
              <span class="caption">Client registration</span>
              <select v-model="httpConfig.registration_method" class="field" aria-label="Client registration" :disabled="Boolean(busy)">
                <option value="auto">Automatic — use approved client ID first</option>
                <option value="manual">Manual — approved client ID</option>
                <option value="cimd">Client ID Metadata Document — public web client</option>
                <option value="dcr">Dynamic registration — provider compatibility</option>
              </select>
            </label>
            <label class="integration-field">
              <span class="caption">Provider metadata</span>
              <select v-model="httpConfig.metadata_mode" class="field" aria-label="Provider metadata" :disabled="Boolean(busy)">
                <option value="discovery">Discovery</option>
                <option value="manual">Manual endpoints</option>
              </select>
            </label>
            <label class="integration-field">
              <span class="caption">Issuer</span>
              <input v-model="httpConfig.issuer" class="field mono" aria-label="Issuer" placeholder="https://login.example.com" :disabled="Boolean(busy)" />
            </label>
            <template v-if="httpConfig.metadata_mode === 'manual'">
              <label v-for="field in endpointFields.filter(field => field.grant === httpConfig.grant_type || !field.grant)" :key="field.key" class="integration-field">
                <span class="caption">{{ field.label }}</span>
                <input v-model="httpConfig[field.key]" class="field mono" :aria-label="field.label" placeholder="https://login.example.com/…" :disabled="Boolean(busy)" />
              </label>
            </template>
            <label class="integration-field">
              <span class="caption">Resource parameter</span>
              <select v-model="httpConfig.resource_parameter" class="field" aria-label="Resource parameter" :disabled="Boolean(busy)">
                <option value="">Provider default</option>
                <option value="resource">resource</option>
                <option value="audience">audience</option>
                <option value="none">None</option>
              </select>
            </label>
            <label v-if="httpConfig.resource_parameter !== 'none'" class="integration-field">
              <span class="caption">Resource value (optional)</span>
              <input v-model="httpConfig.resource_value" class="field mono" aria-label="Resource value" placeholder="Server URL by default" :disabled="Boolean(busy)" />
            </label>
            <label class="integration-field">
              <span class="caption">Token expiry fallback (seconds, optional)</span>
              <input v-model.number="httpConfig.token_expiry_seconds" type="number" min="1" max="31536000" class="field" aria-label="Token expiry fallback" placeholder="Only if the provider omits expires_in" :disabled="Boolean(busy)" />
            </label>
            <label class="integration-field">
              <span class="caption">Authorization parameters (JSON object)</span>
              <textarea v-model="authorizationParamsText" class="field mono" aria-label="Authorization parameters" rows="2" :disabled="Boolean(busy)" placeholder='{"prompt":"consent"}' />
            </label>
            <p class="caption dim">Supported keys: prompt, login_hint, domain_hint, access_type, audience, organization. Use string values; do not include secrets.</p>
            <p v-if="parsedAuthorizationParams === null" class="caption field-hint--bad" role="alert">Enter a JSON object with supported keys and string values.</p>
            <label class="integration-field">
              <span class="caption">Registration access token (write-only, optional)</span>
              <input v-model="httpConfig.registration_access_token" type="password" class="field" aria-label="Registration access token" autocomplete="new-password" :disabled="Boolean(busy)" placeholder="Leave blank to keep a stored token" />
            </label>
          </template>
        </template>
        <template v-else>
          <label class="integration-field"
            ><span class="caption">Arguments (JSON array)</span
            ><textarea
              v-model="argsText"
              class="field mono"
              aria-label="Arguments"
              rows="3"
              :disabled="Boolean(busy)"
              placeholder='["-y", "trusted-mcp", "mcp"]'
            />
          </label>
          <p v-if="parsedArgs === null" class="caption field-hint--bad" role="alert">
            Enter a JSON array of strings (up to 256 arguments, no NUL characters).
          </p>
          <div class="integration-field">
            <span class="caption">Environment variables</span>
            <div v-for="(variable, index) in environment" :key="variable.id" class="mcp-env-row">
              <input
                v-model="variable.key"
                class="field mono"
                :aria-label="`Variable ${index + 1} name`"
                :disabled="variable.stored || Boolean(busy)"
                placeholder="API_KEY"
              />
              <input
                v-model="variable.value"
                class="field mono"
                type="password"
                :aria-label="`Variable ${index + 1} value`"
                :disabled="Boolean(busy)"
                :placeholder="variable.stored ? 'stored — leave blank to keep' : 'Value (literal)'"
                autocomplete="new-password"
              />
              <button
                class="btn btn--sm"
                :aria-label="`Remove variable ${index + 1}`"
                :disabled="Boolean(busy)"
                @click="environment.splice(index, 1)"
              >
                Remove
              </button>
            </div>
            <button class="btn btn--sm btn--outline" :disabled="Boolean(busy)" @click="addVariable">
              Add variable
            </button>
            <p v-if="environmentError" class="caption field-hint--bad" role="alert">{{ environmentError }}</p>
          </div>
          <template v-if="authentication === 'oauth'">
            <label class="integration-field"
              ><span class="caption">CLI adapter</span
              ><select
                v-model="oauthProvider"
                class="field"
                aria-label="CLI adapter"
                :disabled="Boolean(busy)"
                @change="configureCli"
              >
                <option value="generic">Generic OAuth CLI</option>
                <option value="workiq">WorkIQ CLI</option>
              </select></label
            >
            <template v-if="oauthProvider === 'generic'">
              <label class="integration-field"
                ><span class="caption">Login arguments</span
                ><textarea
                  v-model="loginArgsText"
                  class="field mono"
                  aria-label="Login arguments"
                  rows="2"
                  :disabled="Boolean(busy)"
                />
              </label>
              <label class="integration-field"
                ><span class="caption">Setup commands</span
                ><textarea
                  v-model="setupArgsText"
                  class="field mono"
                  aria-label="Setup commands"
                  rows="2"
                  :disabled="Boolean(busy)"
                />
              </label>
            </template>
            <label v-else-if="!eulaStored" class="integration-field row"
              ><input
                v-model="acceptEula"
                type="checkbox"
                aria-label="Accept WorkIQ EULA"
                :disabled="Boolean(busy)"
              /><span class="caption"
                >I accept the
                <a :href="capabilities.workiq?.eula_url" target="_blank" rel="noopener">WorkIQ CLI EULA</a
                >.</span
              ></label
            >
          </template>
          <p class="caption">
            Only run trusted packages: they share agentgateway's container, network, data and credentials. Use
            environment variables for secrets, not visible arguments.
          </p>
        </template>
      </details>
      <p v-if="!editing" class="caption dim">
        If sign-in is required, your browser will open. Account access and tools are shared across this
        instance.
      </p>
      <p v-if="stage && busy" class="caption" role="status">{{ stage }}</p>
      <p v-if="browserUrl && busy" class="caption">
        <a :href="browserUrl" target="_blank" rel="noopener noreferrer">Open sign-in page</a>
        <span class="dim"> if your browser did not open.</span>
      </p>
      <p v-if="error" class="caption field-hint--bad" role="alert">{{ error }}</p>
      <div class="row integration-actions">
        <button class="btn btn--sm" @click="cancel">Cancel</button
        ><button class="btn btn--sm btn--primary" :disabled="Boolean(busy) || !valid" @click="connect">
          {{ busy ? 'Connecting…' : error ? 'Try again' : editing ? 'Save connection' : 'Connect' }}
        </button>
      </div>
    </div>

    <p v-if="!state.servers.length && !savedLogins.length && !loading" class="caption dim">
      No MCP server is configured.
    </p>
    <div v-for="server in state.servers" :key="server.name" class="integration-card">
      <div class="row">
        <span class="material-icons integration-card__icon">hub</span
        ><strong class="grow">{{ server.name }}</strong
        ><span class="chip" :class="server.tool_count ? 'chip--success' : 'chip--warning'"
          >{{ server.tool_count }} tools</span
        >
      </div>
      <p v-if="server.url" class="caption dim mono truncate">{{ server.url }}</p>
      <p v-if="server.oauth" class="caption dim">
        {{
          server.oauth.status === 'connected' || server.oauth.mode === 'cli'
            ? 'Signed in'
            : 'Sign-in required'
        }}
      </p>
      <details class="integration-details">
        <summary>Connection details</summary>
        <p class="caption dim">
          {{ server.transport === 'stdio' ? 'stdio · runs in agentgateway' : 'HTTP' }} ·
          {{ credentialLabel(server.credential || { mode: 'none' }) }}
        </p>
        <p v-if="server.command" class="caption mono">
          {{ server.command }} {{ JSON.stringify(server.args || []) }}
        </p>
        <p v-if="!server.managed" class="caption dim">Managed by gateway configuration</p>
      </details>
      <p v-if="tests[server.name]" class="caption">{{ tests[server.name].message }}</p>
      <div v-if="server.managed" class="row integration-actions">
        <button
          v-if="server.oauth"
          class="btn btn--sm btn--primary"
          :disabled="Boolean(busy) || Boolean(store.mcpAuth.busyName)"
          @click="actions.openMcpLogin(server)"
        >
          Sign in again
        </button>
        <button class="btn btn--sm" :disabled="Boolean(busy)" @click="openForm(server)">
          Connection settings
        </button>
        <button class="btn btn--sm btn--outline" :disabled="Boolean(busy)" @click="test(server)">Test</button
        ><span class="grow" /><button
          class="btn btn--sm btn--danger"
          :disabled="Boolean(busy)"
          @click="remove(server)"
        >
          Remove
        </button>
      </div>
    </div>
    <div v-for="item in savedLogins" :key="item.name" class="integration-card">
      <div class="row">
        <strong class="grow">{{ item.name }}</strong
        ><span class="chip chip--warning">{{
          item.status === 'storage_unavailable' ? 'Needs attention' : 'Sign-in required'
        }}</span>
      </div>
      <p class="caption dim mono truncate">{{ item.url }}</p>
      <p v-if="item.status === 'storage_unavailable'" class="caption">
        Restore the backend credential-key backup.
      </p>
      <div class="row integration-actions">
        <button
          v-if="item.status !== 'storage_unavailable'"
          class="btn btn--sm btn--primary"
          :disabled="Boolean(store.mcpAuth.busyName) || !state.writable"
          @click="actions.openMcpLogin(item)"
        >
          Login</button
        ><span class="grow" /><button class="btn btn--sm btn--danger" @click="remove(item)">
          Remove saved connection
        </button>
      </div>
    </div>
  </div>
  <div id="tool-catalog" class="setting">
    <div class="setting__label">Tool catalog</div>
    <input
      v-model="toolQuery"
      class="field"
      type="search"
      placeholder="Search tools…"
      aria-label="Search tool catalog"
    />
    <p v-if="!toolGroups.length" class="caption dim">
      {{ toolQuery ? 'No matching tools.' : 'No MCP server answered.' }}
    </p>
    <details
      v-for="group in toolGroups"
      :key="group.name"
      class="settings-disclosure model-catalog"
      :open="Boolean(toolQuery)"
    >
      <summary>
        <span class="grow">{{ group.name }}</span
        ><span class="chip chip--success">{{ group.tools.length }} tools</span>
      </summary>
      <div v-for="tool in group.tools" :key="tool.name" class="line line--stacked line--nested">
        <span class="mono">{{ tool.name }}</span
        ><span class="caption dim">{{ tool.description }}</span>
      </div>
    </details>
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { useQuasar } from 'quasar'
import DeclaredField from './DeclaredField.vue'
import { credentialLabel, draftIsValid, resetDraft } from './fields'
import { actions, store } from '../../store'
import { api, server as instance } from '../../api'
import { account } from '../../auth-session.js'
import { desktopOAuth, oauthConnection } from '../../mcp-oauth.js'
import { hostedOAuthDraft, hostedOAuthPayload, parseAuthorizationParams } from '../../mcp-oauth-config.js'
const $q = useQuasar()
const state = reactive({ servers: [], fields: [], stdio_fields: [], oauth_connections: {}, writable: false })
const capabilities = reactive({})
const draft = reactive({}),
  tests = reactive({})
const adding = ref(false),
  editing = ref(''),
  advanced = ref(false),
  loading = ref(false),
  busy = ref('')
const error = ref(''),
  stage = ref(''),
  browserUrl = ref(''),
  toolQuery = ref(''),
  transport = ref('http'),
  authentication = ref('auto')
const httpConfig = reactive(hostedOAuthDraft())
const storedOAuth = ref({}), authorizationParamsText = ref('{}')
const parsedAuthorizationParams = computed(() => parseAuthorizationParams(authorizationParamsText.value))
const endpointFields = [
  { key: 'authorization_endpoint', label: 'Authorization endpoint', grant: 'authorization_code' },
  { key: 'token_endpoint', label: 'Token endpoint' },
  { key: 'device_authorization_endpoint', label: 'Device authorization endpoint', grant: 'device_code' },
]
const argsText = ref('[]'),
  environment = ref([]),
  loginArgsText = ref('[]'),
  setupArgsText = ref('[]'),
  oauthProvider = ref('generic'),
  acceptEula = ref(false),
  eulaStored = ref(false)
let nextVariable = 0,
  controller = null,
  attempt = 0
const context = () => `${instance.url}|${account.user?.id}|${account.sessionId}`
const basicFields = computed(() =>
  transport.value === 'stdio' ? state.stdio_fields : state.fields.filter((field) => field.key !== 'token'),
)
const tokenField = computed(() => state.fields.find((field) => field.key === 'token'))
const editingCredential = computed(
  () => state.servers.find((server) => server.name === editing.value)?.credential,
)
function parse(text) {
  try {
    const value = JSON.parse(text)
    return Array.isArray(value) &&
      value.length <= 256 &&
      value.every((v) => typeof v === 'string' && v.length <= 8192 && !v.includes('\u0000'))
      ? value
      : null
  } catch {
    return null
  }
}
const parsedArgs = computed(() => parse(argsText.value))
const parsedSetup = computed(() => {
  try {
    const value = JSON.parse(setupArgsText.value)
    return Array.isArray(value) && value.length <= 4 && value.every((v) => parse(JSON.stringify(v)) !== null)
      ? value
      : null
  } catch {
    return null
  }
})
const environmentError = computed(() => {
  const keys = environment.value.map((v) => v.key)
  if (
    new Set([...keys, 'HOME', 'PATH']).size > 128 ||
    new Set(keys).size !== keys.length ||
    keys.some((key) => !/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key))
  )
    return 'Use unique valid environment variable names (at most 128).'
  return environment.value.some((v) => v.value.length > 32768 || v.value.includes('\u0000'))
    ? 'Invalid environment value.'
    : ''
})
const valid = computed(
  () =>
    draftIsValid(basicFields.value, draft) &&
    (transport.value !== 'http' || authentication.value === 'token' || parsedAuthorizationParams.value !== null) &&
    (transport.value !== 'stdio' ||
      (parsedArgs.value !== null &&
        !environmentError.value &&
        (authentication.value !== 'oauth' ||
          (oauthProvider.value === 'workiq'
            ? acceptEula.value || eulaStored.value
            : parse(loginArgsText.value) !== null && parsedSetup.value !== null)))),
)
const savedLogins = computed(() =>
  Object.entries(state.oauth_connections || {})
    .filter(([name]) => !state.servers.some((server) => server.name === name))
    .map(([name, item]) => ({ name, ...item })),
)
const toolGroups = computed(() => {
  const query = toolQuery.value.toLowerCase()
  return (store.catalog.tool_servers || [])
    .map((group) => ({
      ...group,
      tools: (store.catalog.tools || []).filter(
        (tool) =>
          tool.server === group.name &&
          `${tool.name} ${tool.description || ''}`.toLowerCase().includes(query),
      ),
    }))
    .filter((group) => !query || group.tools.length)
})
function addVariable() {
  environment.value.push({ id: nextVariable++, key: '', value: '', stored: false })
}
async function load() {
  loading.value = true
  try {
    Object.assign(state, await api.mcpServers())
    Object.assign(capabilities, await api.mcpOAuthCapabilities().catch(() => ({})))
  } catch (failure) {
    error.value = failure.message
  } finally {
    loading.value = false
  }
}
async function refresh() {
  await Promise.all([load(), actions.loadCatalog(true), actions.loadMcpAuth()])
}
function openForm(server) {
  adding.value = true
  editing.value = server?.name || ''
  transport.value = server?.transport || 'http'
  authentication.value = server?.oauth ? 'oauth' : server?.credential?.mode === 'stored' ? 'token' : 'auto'
  advanced.value = Boolean(server)
  error.value = stage.value = browserUrl.value = ''
  resetDraft(draft, [...state.fields, ...state.stdio_fields], server || {})
  storedOAuth.value = {
    client_secret_configured: Boolean(server?.oauth?.client_secret_configured),
    private_key_configured: Boolean(server?.oauth?.private_key_configured),
  }
  Object.assign(httpConfig, hostedOAuthDraft(server?.oauth))
  if (!server?.oauth && !desktopOAuth() && capabilities.http?.web_callback) httpConfig.callback_mode = 'web'
  authorizationParamsText.value = JSON.stringify(server?.oauth?.authorization_params || {}, null, 2)
  argsText.value = JSON.stringify(server?.args || [], null, 2)
  environment.value = Object.keys(server?.env || {}).map((key) => ({
    id: nextVariable++,
    key,
    value: '',
    stored: true,
  }))
  oauthProvider.value = server?.oauth?.provider || 'generic'
  loginArgsText.value = JSON.stringify(server?.oauth?.login_args || [])
  setupArgsText.value = JSON.stringify(server?.oauth?.setup_args || [])
  eulaStored.value = server?.oauth?.accept_eula === true
  acceptEula.value = false
}
function configureCli() {
  if (oauthProvider.value === 'workiq') {
    draft.command = 'npx'
    argsText.value = JSON.stringify(capabilities.workiq?.args || ['-y', '@microsoft/workiq@1.0.1', 'mcp'])
    eulaStored.value = false
  }
}
function clearOAuthSecrets() {
  httpConfig.client_secret = httpConfig.private_key = httpConfig.registration_access_token = ''
}
function cancel() {
  clearOAuthSecrets()
  const name = busy.value
  attempt++
  controller?.cancel()
  controller = null
  adding.value = false
  busy.value = ''
  error.value = stage.value = browserUrl.value = ''
  if (name) actions.finishMcpLogin(name)
}
async function connect() {
  const name = draft.name,
    id = ++attempt,
    initial = context()
  busy.value = name
  error.value = browserUrl.value = ''
  actions.beginMcpLogin(name)
  try {
    let config
    if (transport.value === 'stdio') {
      config = {
        transport: 'stdio',
        command: draft.command,
        args: parsedArgs.value,
        env: Object.fromEntries(environment.value.map((v) => [v.key, v.stored && !v.value ? null : v.value])),
      }
      if (authentication.value !== 'oauth') {
        Object.assign(state, await api.saveMcpServer(name, config))
        config = null
      } else
        config = {
          ...config,
          provider: oauthProvider.value,
          login_args: parse(loginArgsText.value),
          setup_args: parsedSetup.value,
          accept_eula: acceptEula.value || eulaStored.value,
        }
    } else {
      stage.value = 'Checking connection…'
      const result = await api.connectMcpServer(name, {
        transport: 'http',
        url: draft.url,
        authentication: authentication.value,
        ...(authentication.value === 'token'
          ? { token: draft.token }
          : hostedOAuthPayload(httpConfig, parsedAuthorizationParams.value)),
      })
      if (id === attempt && initial === context()) clearOAuthSecrets()
      config = result.status === 'oauth_required' ? result.config : null
    }
    if (id !== attempt || initial !== context()) return
    if (config) {
      controller = oauthConnection({ backend: api, context, onBrowser: url => {
        if (id === attempt && initial === context()) browserUrl.value = url
      } })
      await controller.connect(name, config, (value) => {
        if (id === attempt) stage.value = value
      })
    }
    if (id !== attempt) return
    await refresh()
    adding.value = false
    controller = null
    $q.notify({ type: 'positive', message: `${name} connected` })
  } catch (failure) {
    if (id === attempt) {
      error.value = failure.message
      if (failure.detail?.code === 'oauth_client_required') {
        authentication.value = 'oauth'
        advanced.value = true
      }
    }
  } finally {
    if (id === attempt) {
      busy.value = ''
      actions.finishMcpLogin(name)
    }
  }
}
function remove(server) {
  $q.dialog({
    title: `Remove ${server.name}`,
    message:
      'Its tools and local connection credentials will be removed. Revoke provider consent separately if needed.',
    cancel: true,
  }).onOk(async () => {
    busy.value = server.name
    try {
      Object.assign(state, await api.removeMcpServer(server.name))
      await refresh()
    } catch (failure) {
      error.value = failure.message
    } finally {
      busy.value = ''
    }
  })
}
async function test(server) {
  busy.value = server.name
  try {
    tests[server.name] = await api.testMcpServer(server.name)
  } catch (failure) {
    tests[server.name] = { message: failure.message }
  } finally {
    busy.value = ''
  }
}
onMounted(load)
onBeforeUnmount(cancel)
</script>
<style scoped>
.mcp-env-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.mcp-env-row .field {
  flex: 1 1 150px;
  min-width: 0;
}
.integration-add > details {
  margin-block: 12px;
}
</style>
