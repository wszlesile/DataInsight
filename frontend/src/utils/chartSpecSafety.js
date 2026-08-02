export function applyPieLegendSafety(option) {
  if (!Array.isArray(option?.series)) return
  const pieSeries = option.series.filter((series) => series && series.type === 'pie')
  if (!pieSeries.length) return

  const legend = option.legend && typeof option.legend === 'object'
    ? (Array.isArray(option.legend) ? option.legend[0] : option.legend)
    : null
  if (legend && legend.show !== false) {
    const names = []
    pieSeries.forEach((series) => {
      ;(Array.isArray(series.data) ? series.data : []).forEach((item) => {
        const name = item && typeof item === 'object' ? String(item.name || '') : ''
        if (name && !names.includes(name)) names.push(name)
      })
    })
    legend.data = names
    legend.orient = 'horizontal'
    legend.left = 'center'
    legend.bottom = 8
    delete legend.right
    delete legend.top
    delete legend.height
  }

  pieSeries.forEach((series) => {
    series.center = ['50%', '48%']
    series.radius = ['28%', '60%']
  })
}
