const markdownImagePattern = /!\[([^\]]*)\]\(([^)\r\n]*)\)/g

const escapeHtmlText = (value) => String(value || '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')

export function replaceUnsupportedMarkdownImages(content) {
  return String(content || '').replace(markdownImagePattern, (_match, rawAlt, rawDestination) => {
    const alt = escapeHtmlText(String(rawAlt || '').trim())
    const destination = String(rawDestination || '').trim()
    if (/^chart:/i.test(destination)) {
      const label = alt || destination.slice(destination.indexOf(':') + 1).trim()
      return label ? `（图表：${label}）` : '（图表产物）'
    }
    return alt || '（图片产物暂不支持）'
  })
}
