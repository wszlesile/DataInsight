import test from 'node:test'
import assert from 'node:assert/strict'
import { marked } from 'marked'

import { replaceUnsupportedMarkdownImages } from './reportMarkdown.js'

test('renders chart image placeholders as captions instead of broken image URLs', () => {
  const report = [
    '## 可视化图表',
    '![设备类型分布](chart:设备类型分布)',
    '![](chart:状态图)'
  ].join('\n')

  assert.equal(
    replaceUnsupportedMarkdownImages(report),
    '## 可视化图表\n（图表：设备类型分布）\n（图表：状态图）'
  )
  assert.doesNotMatch(marked.parse(replaceUnsupportedMarkdownImages(report)), /<img\b/i)
})

test('removes unsupported external image requests while retaining their alt text', () => {
  assert.equal(
    replaceUnsupportedMarkdownImages('![现场照片](https://example.invalid/device.png)'),
    '现场照片'
  )
})

test('escapes HTML in unsupported image alt text', () => {
  assert.equal(
    replaceUnsupportedMarkdownImages('![<img src=x>](chart:unsafe)'),
    '（图表：&lt;img src=x&gt;）'
  )
})
