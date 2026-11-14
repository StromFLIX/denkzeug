import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { randomBytes, createCipheriv, createDecipheriv } from 'node:crypto'
const { createAccountStore, accountOrigin } = createRequire(import.meta.url)('../desktop/account-store.cjs')

test('native desktop vault encrypts at rest, scopes servers, rotates and removes credentials', t => {
  const dir = mkdtempSync(join(tmpdir(), 'denkzeug-vault-')), file = join(dir, 'sessions.json'), key = randomBytes(32)
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  // Substitute the OS encryption primitive, not the persistence implementation.
  const safeStorage = {
    isEncryptionAvailable: () => true,
    getSelectedStorageBackend: () => 'gnome_libsecret',
    encryptString (value) { const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv); const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]); return Buffer.concat([iv, cipher.getAuthTag(), encrypted]) },
    decryptString (value) { const decipher = createDecipheriv('aes-256-gcm', key, value.subarray(0, 12)); decipher.setAuthTag(value.subarray(12, 28)); return Buffer.concat([decipher.update(value.subarray(28)), decipher.final()]).toString('utf8') }
  }
  const vault = createAccountStore({ safeStorage, file })
  vault.set({ origin: 'https://one.test', value: 'refresh-super-secret' })
  assert.equal(readFileSync(file, 'utf8').includes('refresh-super-secret'), false)
  assert.equal(statSync(file).mode & 0o777, 0o600)
  assert.equal(vault.get({ origin: 'https://one.test' }).value, 'refresh-super-secret')
  assert.equal(vault.get({ origin: 'https://two.test' }).value, '')
  vault.set({ origin: 'https://one.test', value: 'rotated' })
  assert.equal(vault.get({ origin: 'https://one.test' }).value, 'rotated')
  vault.remove({ origin: 'https://one.test' })
  assert.equal(vault.get({ origin: 'https://one.test' }).value, '')
})
test('native vault refuses Linux plaintext fallback, unavailable OS storage and invalid origins', () => {
  for (const available of [true, false]) {
    const vault = createAccountStore({ safeStorage: { isEncryptionAvailable: () => available, getSelectedStorageBackend: () => 'basic_text' }, file: '/unused' })
    assert.throws(() => vault.get({ origin: 'https://example.test' }), /OS credential store/)
  }
  for (const value of ['http://example.test', 'https://user:pass@example.test', 'https://example.test/path', 'https://example.test#secret', 'https://example.test?token=x', 'file:///tmp/x']) assert.throws(() => accountOrigin(value))
})
test('Capacitor bridge wires device-only Keychain and authenticated Keystore ciphertext without backup', () => {
  const ios = readFileSync(new URL('../native/ios/BridgeViewController.swift', import.meta.url), 'utf8')
  const android = readFileSync(new URL('../native/android/DenkzeugAccountStorePlugin.java', import.meta.url), 'utf8')
  const activity = readFileSync(new URL('../native/android/MainActivity.java', import.meta.url), 'utf8')
  assert.match(ios, /registerPluginInstance\(store\)/)
  assert.match(ios, /forMainFrameOnly: true/)
  assert.match(ios, /call.getString\("capability"\) == capability/)
  assert.match(ios, /kSecAttrAccessibleWhenUnlockedThisDeviceOnly/)
  assert.match(ios, /SecItemCopyMatching/)
  assert.match(ios, /SecItemUpdate/)
  assert.match(android, /AndroidKeyStore/)
  assert.match(android, /DOCUMENT_START_SCRIPT/)
  assert.match(android, /window === window.top/)
  assert.match(android, /!capability.equals\(call.getString\("capability"\)\)/)
  assert.match(android, /Collections.singleton\("https:\/\/localhost"\)/)
  assert.match(android, /AES\/GCM\/NoPadding/)
  assert.match(android, /updateAAD\(origin/)
  assert.match(activity, /registerPlugin\(DenkzeugAccountStorePlugin.class\)/)
  const sync = readFileSync(new URL('./sync-android.mjs', import.meta.url), 'utf8')
  assert.match(sync, /android:allowBackup="false"/)
})
