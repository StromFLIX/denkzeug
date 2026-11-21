import { App } from '@modelcontextprotocol/ext-apps'

const app = new App({ name: 'Weekly briefing', version: '1.0.0' })
const filter = document.querySelector('select')
let items = []
function render () {
  const rows = items.filter(({ group }) => !filter.value || group === filter.value)
  const list = document.querySelector('ul')
  list.replaceChildren(...rows.map(({ title, group }) => {
    const row = document.createElement('li')
    const badge = document.createElement('span')
    badge.textContent = group
    row.append(title, badge)
    return row
  }))
}
filter.addEventListener('change', render)
app.ontoolresult = ({ structuredContent }) => {
  items = structuredContent.items
  render()
}
app.connect().catch(error => console.error(error))
