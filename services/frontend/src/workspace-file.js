import createPurifier from 'dompurify'

const raster = new Map([
  ['png', 'image/png'], ['jpeg', 'image/jpeg'], ['jpg', 'image/jpeg'], ['webp', 'image/webp'],
  ['gif', 'image/gif'], ['avif', 'image/avif'], ['bmp', 'image/bmp']
])
const presentations = new Set([
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.oasis.opendocument.presentation', 'application/vnd.ms-powerpoint'
])
export function fileViewer (file) {
  const mime = file.mime_type?.split(';')[0].trim().toLowerCase() || ''
  const ext = file.name?.toLowerCase().split('.').pop()
  const generic = !mime || ['application/octet-stream', 'application/zip', 'text/plain'].includes(mime)
  if (mime === 'application/pdf' || (generic && ext === 'pdf')) return { kind: 'pdf' }
  if (mime === 'text/html' || (generic && ['html', 'htm'].includes(ext))) return { kind: 'html' }
  if (mime === 'text/markdown' || mime === 'text/x-markdown' || (generic && ['md', 'markdown'].includes(ext))) return { kind: 'markdown' }
  if (presentations.has(mime) || (generic && ['pptx', 'ppt', 'odp'].includes(ext))) return { kind: 'slides' }
  if ([...raster.values()].includes(mime)) return { kind: 'image', mime }
  if (generic && raster.has(ext)) return { kind: 'image', mime: raster.get(ext) }
  if (file.previewable) return { kind: 'image', mime: 'image/png', preview: true }
  if (mime === 'text/plain' || (generic && ext === 'txt')) return { kind: 'text' }
  return { kind: 'download' }
}

// A presentation publication can supply ordered slide images or a PDF export.
export function presentationAssets (files) {
  const slides = files.filter(file => file.role === 'preview' && file.previewable)
  const pdf = files.find(file => fileViewer(file).kind === 'pdf')
  return { slides, pdf }
}

const policy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"
let purifier
export function workspaceHtml (text) {
  if (!purifier) {
    purifier = createPurifier(window)
    purifier.addHook('uponSanitizeAttribute', (node, attr) => {
      if (!['href', 'xlink:href', 'src'].includes(attr.attrName)) return
      const value = attr.attrValue.trim()
      // Fragment links and embedded raster images don't navigate out or make requests.
      attr.keepAttr = (attr.attrName !== 'src' && /^#[\w.:-]*$/.test(value)) ||
        (/^(img|image)$/i.test(node.localName) && /^data:image\/(?:png|jpeg|gif|webp|avif);base64,/i.test(value))
    })
  }
  const body = purifier.sanitize(text, {
    WHOLE_DOCUMENT: true,
    USE_PROFILES: { html: true, svg: true, svgFilters: true },
    ADD_TAGS: ['style'],
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'base', 'link', 'meta', 'form', 'foreignobject', 'animate', 'animatemotion', 'animatetransform', 'set'],
    FORBID_ATTR: ['target', 'download', 'srcset', 'ping', 'action', 'formaction', 'poster', 'background', 'attributionsrc']
  })
  return '<!doctype html>' + body.replace(/<head\b[^>]*>/i, head => `${head}<meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="viewport" content="width=device-width, initial-scale=1">`)
}