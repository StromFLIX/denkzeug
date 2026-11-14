const { readFileSync, writeFileSync, renameSync, existsSync } = require('node:fs')

function accountOrigin (value) {
  const url = new URL(value)
  if (url.protocol !== 'https:' || url.username || url.password || url.origin !== value) throw new Error('Invalid account origin')
  return url.origin
}

/** Electron safeStorage uses Keychain, DPAPI, or the Linux secret service.
 * Refuse Linux basic_text: it only obfuscates using a hard-coded password. */
function createAccountStore ({ safeStorage, file }) {
  function ready () {
    if (!safeStorage.isEncryptionAvailable() || safeStorage.getSelectedStorageBackend?.() === 'basic_text') throw new Error('Unlock your OS credential store to keep this account signed in.')
  }
  function read () {
    if (!existsSync(file)) return {}
    return JSON.parse(readFileSync(file, 'utf8'))
  }
  function save (data) {
    const temporary = `${file}.tmp`
    writeFileSync(temporary, JSON.stringify(data), { mode: 0o600 })
    renameSync(temporary, file)
  }
  return {
    get ({ origin }) { ready(); const encrypted = read()[accountOrigin(origin)]; return { value: encrypted ? safeStorage.decryptString(Buffer.from(encrypted, 'base64')) : '' } },
    set ({ origin, value }) {
      ready(); origin = accountOrigin(origin)
      if (typeof value !== 'string' || !value || value.length > 16384) throw new Error('Invalid account credential')
      const data = read(); data[origin] = safeStorage.encryptString(value).toString('base64'); save(data)
      return {}
    },
    remove ({ origin }) { origin = accountOrigin(origin); const data = read(); delete data[origin]; save(data); return {} }
  }
}
module.exports = { createAccountStore, accountOrigin }
