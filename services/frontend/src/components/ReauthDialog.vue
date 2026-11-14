<template>
  <q-dialog v-model="open" persistent @hide="cancel">
    <q-card class="reauth-card">
      <h2>Confirm it’s you</h2>
      <p class="muted">Enter your password to continue this sensitive change.</p>
      <form class="account-form" @submit.prevent="confirm">
        <label>Password<input ref="input" v-model="password" class="field" type="password" autocomplete="current-password" required :disabled="busy" /></label>
        <p v-if="error" role="alert" class="account-error">{{ error }}</p>
        <div class="row"><button type="button" class="btn" :disabled="busy" @click="cancel">Cancel</button><button class="btn btn--primary" :disabled="busy">{{ busy ? 'Checking…' : 'Continue' }}</button></div>
      </form>
    </q-card>
  </q-dialog>
</template>
<script setup>
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { setReauthHandler } from '../api.js'
import { accountApi } from '../account.js'
import { account } from '../auth-session.js'
const open = ref(false), password = ref(''), error = ref(''), busy = ref(false), input = ref(null)
let pending = null
setReauthHandler(() => {
  if (pending) return pending.promise
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  pending = { promise, resolve, reject }
  password.value = error.value = ''
  open.value = true
  nextTick(() => input.value?.focus())
  return promise
})
function cancel () { pending?.reject(new Error('Change cancelled.')); pending = null; open.value = false; password.value = '' }
async function confirm () {
  busy.value = true; error.value = ''
  try { await accountApi.reauth(password.value); const done = pending; pending = null; open.value = false; password.value = ''; done?.resolve() } catch (e) { error.value = e.message } finally { busy.value = false }
}
watch(() => account.epoch, cancel)
onUnmounted(() => { cancel(); setReauthHandler(null) })
</script>
<style scoped>.reauth-card { width: min(28rem, 94vw); padding: 1.5rem; } .reauth-card h2 { font-size: 1.2rem; }</style>
