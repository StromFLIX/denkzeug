import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { configureAndroidStrings } from './native-brand.mjs'

// Keep generated Gradle files out of Git while retaining our small native bridge.
// Run after cap add/sync so the activity replacement cannot be overwritten.
const root = new URL('../', import.meta.url)
const config = JSON.parse(await readFile(new URL('capacitor.config.json', root), 'utf8'))
if (config.appId !== 'dev.nautionette.app') throw new Error('Update the native Java package when changing appId.')
// Capacitor consumes WebKit as an implementation dependency, not an API export.
// Our app-owned main-frame vault plugin needs its own compile dependency too.
const gradlePath = new URL('android/app/build.gradle', root)
let gradle = await readFile(gradlePath, 'utf8')
const webkitDependency = "implementation 'androidx.webkit:webkit:1.14.0'"
if (!gradle.includes(webkitDependency)) {
  if (!gradle.includes('dependencies {')) throw new Error('Android app dependency block is missing.')
  gradle = gradle.replace('dependencies {', `dependencies {\n    ${webkitDependency}`)
  await writeFile(gradlePath, gradle)
}
for (const name of ['MainActivity.java', 'NautionetteSystemBarsPlugin.java', 'NautionetteDownloadsPlugin.java', 'DenkzeugAccountStorePlugin.java']) {
  await copyFile(new URL(`native/android/${name}`, root), new URL(`android/app/src/main/java/dev/nautionette/app/${name}`, root))
}
// BridgeWebChromeClient requests BOTH permissions for AUDIO_CAPTURE and denies
// getUserMedia if either is missing, even when the user allows RECORD_AUDIO.
// MODIFY_AUDIO_SETTINGS is a normal (install-time) permission, not another prompt.
const manifestPath = new URL('android/app/src/main/AndroidManifest.xml', root)
let manifest = await readFile(manifestPath, 'utf8')
for (const permission of ['RECORD_AUDIO', 'MODIFY_AUDIO_SETTINGS']) {
  if (!manifest.includes(`android.permission.${permission}`)) {
    manifest = manifest.replace('</manifest>', `    <uses-permission android:name="android.permission.${permission}" />\n</manifest>`)
  }
}
// Account credentials are device-bound; never restore ciphertext without its Keystore key.
manifest = manifest.replace(/android:allowBackup="[^"]*"/, 'android:allowBackup="false"')
if (!manifest.includes('android:allowBackup=')) manifest = manifest.replace('<application', '<application android:allowBackup="false"')
await writeFile(manifestPath, manifest)
// Keep the app and launcher/activity labels in sync without touching package IDs,
// custom_url_scheme or permission identifiers in the generated Android project.
const stringsPath = new URL('android/app/src/main/res/values/strings.xml', root)
await writeFile(stringsPath, configureAndroidStrings(await readFile(stringsPath, 'utf8')))
// Android downscales the highest-density approved artwork. Remove only the
// generated Capacitor launcher bitmaps, not unrelated resources or custom IDs.
const resources = new URL('android/app/src/main/res/', root)
for (const density of ['mdpi', 'hdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi']) {
  for (const name of ['ic_launcher', 'ic_launcher_round', 'ic_launcher_foreground']) {
    await rm(new URL(`mipmap-${density}/${name}.png`, resources), { force: true })
  }
}
await mkdir(new URL('mipmap-xxxhdpi/', resources), { recursive: true })
for (const name of ['ic_launcher', 'ic_launcher_round', 'ic_launcher_foreground']) {
  const source = name === 'ic_launcher_foreground' ? 'ic_launcher_foreground.png' : 'ic_launcher.png'
  await copyFile(new URL(`native/icons/${source}`, root), new URL(`mipmap-xxxhdpi/${name}.png`, resources))
}
// Full-bleed black + a transparent white-glyph foreground: the launcher owns
// the outer shape. Reusing the padded iOS icon here creates a second rounded
// square inside Android's mask and leaves uneven white slivers around the mark.
await writeFile(new URL('values/ic_launcher_background.xml', resources), '<?xml version="1.0" encoding="utf-8"?>\n<resources><color name="ic_launcher_background">#000000</color></resources>\n')
