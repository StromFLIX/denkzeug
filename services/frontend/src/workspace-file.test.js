import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fileViewer, presentationAssets } from './workspace-file.js'

const file = (name, mime_type, extras = {}) => ({ name, mime_type, ...extras })

test('recognized original formats and older generic MIME metadata pick a viewer', () => {
  assert.deepEqual(fileViewer(file('notes.md', 'text/plain; charset=UTF-8')), { kind: 'markdown' })
  assert.deepEqual(fileViewer(file('page.HTML', 'application/octet-stream')), { kind: 'html' })
  assert.deepEqual(fileViewer(file('manual.PDF')), { kind: 'pdf' })
  assert.deepEqual(fileViewer(file('image.jpg', 'image/jpeg')), { kind: 'image', mime: 'image/jpeg' })
  assert.deepEqual(fileViewer(file('intro.pptx', 'application/zip')), { kind: 'slides' })
  assert.deepEqual(fileViewer(file('README', 'text/plain')), { kind: 'text' })
})

test('unsupported formats use only a safe stored raster or offer download', () => {
  assert.deepEqual(fileViewer(file('drawing.svg', 'image/svg+xml', { previewable: true })), { kind: 'image', mime: 'image/png', preview: true })
  assert.deepEqual(fileViewer(file('drawing.svg', 'image/svg+xml')), { kind: 'download' })
  assert.deepEqual(fileViewer(file('legacy.zip', 'application/zip')), { kind: 'download' })
})

test('slide navigation uses the publication order and does not include unrelated originals', () => {
  const two = file('slide-2.png', 'image/png', { role: 'preview', previewable: true })
  const one = file('slide-1.png', 'image/png', { role: 'preview', previewable: true })
  const pdf = file('deck.pdf', 'application/pdf')
  assert.deepEqual(presentationAssets([two, file('photo.png', 'image/png', { role: 'file', previewable: true }), pdf, one]), { slides: [two, one], pdf })
})
