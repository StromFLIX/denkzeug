import { onUnmounted, ref } from 'vue'
import { copyText } from './clipboard'

export function useCopyStatus () {
  const copyStatus = ref('')
  let copyTimer
  onUnmounted(() => clearTimeout(copyTimer))

  async function copy (text, label) {
    clearTimeout(copyTimer)
    copyStatus.value = ''
    try {
      await copyText(text)
      copyStatus.value = `${label} copied`
    } catch {
      copyStatus.value = 'Could not copy. Select the text and copy it manually.'
    }
    copyTimer = setTimeout(() => { copyStatus.value = '' }, 3000)
  }

  return { copy, copyStatus }
}
