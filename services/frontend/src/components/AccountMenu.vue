<template>
  <button class="account-menu" aria-label="Account menu" :title="account.user?.username">
    <span class="material-icons" aria-hidden="true">account_circle</span><span class="account-menu__label">Account</span>
    <q-menu class="pick-menu">
      <div class="account-menu__identity"><strong>{{ account.user?.display_name || account.user?.username }}</strong><p>@{{ account.user?.username }} · {{ account.user?.role }}</p></div>
      <RouterLink v-close-popup to="/settings/account" class="pick-menu__item">Profile &amp; security</RouterLink>
      <RouterLink v-if="account.user?.role === 'admin'" v-close-popup to="/settings/users" class="pick-menu__item">Users &amp; invitations</RouterLink>
      <RouterLink v-close-popup to="/settings/appearance" class="pick-menu__item">Appearance</RouterLink>
      <button v-close-popup class="pick-menu__item" @click="logout">Sign out</button>
      <button v-if="isNative" v-close-popup class="pick-menu__item" @click="switchServer">Switch workspace</button>
    </q-menu>
  </button>
</template>
<script setup>
import { useQuasar } from 'quasar'
import { account, isNative } from '../auth-session.js'
import { accountApi } from '../account.js'
const $q = useQuasar()
async function logout () { try { await accountApi.logout() } catch (e) { $q.notify({ type: 'negative', message: e.message }) } }
async function switchServer () { try { await accountApi.switchServer('') } catch (e) { $q.notify({ type: 'negative', message: e.message }) } }
</script>
<style scoped>
.account-menu { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: .25rem; width: calc(100% - 16px); min-height: 3.5rem; border: 0; border-radius: var(--radius-md); background: transparent; color: var(--text-muted); font: inherit; cursor: pointer; touch-action: manipulation; }
.account-menu__label { font-size: .625rem; }
.account-menu:active { background: var(--surface-hover); }
.account-menu:focus-visible { outline: 2px solid var(--accent); }
.account-menu__identity { padding: .8rem 1rem; max-width: 20rem; overflow-wrap: anywhere; border-bottom: 1px solid var(--border); }
.account-menu__identity p { margin: .25rem 0 0; color: var(--text-muted); font-size: .75rem; }
@media (hover: hover) and (pointer: fine) { .account-menu:hover { background: var(--surface-hover); } }
@media (max-width: 900px) { .account-menu { width: auto; padding: 0 .75rem; } }
</style>
