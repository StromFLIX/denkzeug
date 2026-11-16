import { App } from '@modelcontextprotocol/ext-apps'
const app = new App({ name: 'External example app', version: '1' })
let notes = ''
app.ontoolresult = async result => {
  notes = result.structuredContent.notes
  document.querySelector('#result').textContent = notes
  try { await app.callServerTool({ name: 'not-authorized', arguments: {} }) } catch { document.querySelector('#status').textContent = 'tools blocked' }
}
document.querySelector('#link').onclick = () => app.openLink({ url: 'https://github.com/example/project/releases/new' })
document.querySelector('#copy').onclick = () => {
  const text = document.createElement('textarea')
  text.value = notes
  document.body.append(text)
  text.select()
  try { document.execCommand('copy'); document.querySelector('#status').textContent = 'Copied' } catch { document.querySelector('#status').textContent = 'Select the original text to copy' }
  text.remove()
}
app.connect()
