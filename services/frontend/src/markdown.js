import DOMPurify from 'dompurify'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { Marked } from 'marked'
import { highlight } from './highlight'

const marked = new Marked({ gfm: true, breaks: true })

marked.use({
  renderer: {
    code ({ text, lang }) {
      const language = (lang || '').trim().split(/\s+/)[0]
      return `<pre><code class="hljs">${highlight(text, language || 'json')}</code></pre>`
    }
  }
})

// Tokenize before Markdown escapes backslashes or interprets TeX punctuation.
// Marked still consumes code spans and fenced code as whole tokens.
const display = String.raw`\$\$((?:\\[\s\S]|[^\\$]|\$(?!\$))+)\$\$|\\\[((?:\\[^\]]|[^\\])+)\\\]`
const blockMath = new RegExp(`^ {0,3}(?:${display})[ \\t]*(?:\n|$)`)
const displayMath = new RegExp(`^(?:${display})`)
const inlineMath = /^(?:\\\(((?:\\[^)\n]|[^\\\n])+)\\\)|\$(?![\s$])((?:\\[^\n]|[^\\$\n])+(?<!\s))\$(?![\d$]))/
// A dollar amount followed by prose should not open a formula that swallows
// the rest of the sentence up to an unrelated dollar sign.
const currencyInProse = /^\$\d+(?:[.,]\d+)*[.!?,;:]?[ \t]+\p{L}{2}/u

function mathToken (name, match, displayMode) {
  const tex = (match?.[1] ?? match?.[2])?.trim()
  if (tex) return { type: name, raw: match[0], tex, displayMode }
}

function renderMathSlot (token) {
  return `<denkzeug-math>${this.parser.options.mathFormulas.push(token) - 1}</denkzeug-math>`
}

marked.use({
  extensions: [
    {
      name: 'blockMath', level: 'block',
      tokenizer (src) { return mathToken('blockMath', blockMath.exec(src), true) },
      renderer: renderMathSlot
    },
    {
      name: 'inlineMath', level: 'inline',
      start (src) { return /\$|\\[([]/.exec(src)?.index },
      tokenizer (src) {
        return mathToken('inlineMath', displayMath.exec(src), true) ??
          (currencyInProse.test(src) ? undefined : mathToken('inlineMath', inlineMath.exec(src), false))
      },
      renderer: renderMathSlot
    }
  ]
})

// Anything the agent writes is untrusted, so it is parsed then scrubbed.
export function renderMarkdown (text) {
  const formulas = []
  const fragment = DOMPurify.sanitize(marked.parse(text || '', { mathFormulas: formulas }), {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote', 'a', 'span',
      'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'hr',
      'table', 'thead', 'tbody', 'tr', 'th', 'td', 'denkzeug-math'
    ],
    ALLOWED_ATTR: ['href', 'title', 'class'],
    ALLOWED_URI_REGEXP: /^https?:|^mailto:/i,
    ADD_ATTR: ['target', 'rel']
  })

  // Add trusted controls only after sanitizing the model's HTML. Keeping the
  // toolbar outside <pre> leaves it visible when long code scrolls sideways.
  for (const pre of fragment.querySelectorAll('pre')) {
    if (!pre.querySelector('code')) continue
    const block = document.createElement('div')
    block.className = 'code-block'
    const toolbar = document.createElement('div')
    toolbar.className = 'code-block__toolbar'
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'btn btn--sm code-block__copy'
    button.textContent = 'Copy code'
    toolbar.append(button)
    pre.replaceWith(block)
    block.append(toolbar, pre)
  }
  for (const slot of fragment.querySelectorAll('denkzeug-math')) {
    const index = slot.textContent
    const formula = /^\d+$/.test(index) && formulas[Number(index)]
    if (!formula) {
      slot.replaceWith(...slot.childNodes)
      continue
    }
    // Keep KaTeX's generated MathML/SVG and layout styles without allowing
    // those elements or attributes in model-authored HTML. Never trust TeX
    // commands that can assign URLs, load images, or insert HTML attributes.
    const element = document.createElement('span')
    if (formula.displayMode) element.className = 'math-display'
    try {
      if (formula.tex.length <= 4096) {
        katex.render(formula.tex, element, {
          displayMode: formula.displayMode, trust: false, maxExpand: 300, maxSize: 20, strict: 'ignore'
        })
      }
    } catch (error) {
      if (!(error instanceof katex.ParseError || error instanceof RangeError)) throw error
    }
    if (!element.hasChildNodes()) element.textContent = formula.raw.trim()
    slot.replaceWith(element)
  }
  const container = document.createElement('div')
  container.append(fragment)
  return container.innerHTML
}

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})
