<template>
  <div class="chart-display">
    <div v-if="isSpecMode" ref="specContainer" class="chart-canvas" :style="{ height: `${chartHeight}px` }" />
    <div v-else class="chart-placeholder">
      <span class="placeholder-icon">图</span>
      <p>分析图表将在这里展示</p>
    </div>
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { computed, defineExpose, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { applyPieLegendSafety } from '../utils/chartSpecSafety'

const props = defineProps({
  chartSpec: {
    type: Object,
    default: () => null
  }
})
const emit = defineEmits(['point-click'])

const specContainer = ref(null)
const chartInstance = shallowRef(null)
const isSpecMode = computed(() => props.chartSpec && typeof props.chartSpec === 'object' && Object.keys(props.chartSpec).length > 0)
const normalizedChartSpec = computed(() => sanitizeChartSpec(props.chartSpec))
const chartHeight = computed(() => estimateChartHeight(normalizedChartSpec.value))

function formatChartNumber(value) {
  if (value === null || value === undefined || value === '') return ''
  const numericValue = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numericValue)) return value
  if (Number.isInteger(numericValue)) {
    return numericValue.toLocaleString('zh-CN')
  }
  return numericValue.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
}

function escapeTooltipText(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function parseTemporalValue(value) {
  if (value === null || value === undefined || value === '') return null
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value
  if (typeof value === 'number' && Number.isFinite(value)) {
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? null : date
  }
  const text = String(value).trim()
  if (!text) return null
  if (!/^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/.test(text)) {
    return null
  }
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

function isTemporalValue(value) {
  return parseTemporalValue(value) !== null
}

function isTemporalCategoryAxis(axis, categoryData = []) {
  const axisName = String(axis?.name || '').toLowerCase()
  if (/(time|date|timestamp|时间|日期)/.test(axisName)) return true
  const sample = categoryData.filter((item) => item !== null && item !== undefined && item !== '').slice(0, 8)
  return sample.length > 0 && sample.every(isTemporalValue)
}

function hasMultipleTemporalDays(values = []) {
  const days = new Set()
  values.forEach((value) => {
    const date = parseTemporalValue(value)
    if (!date) return
    days.add(`${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`)
  })
  return days.size > 1
}

function padDatePart(value) {
  return String(value).padStart(2, '0')
}

function formatTemporalValue(value, mode = 'axis', options = {}) {
  const date = parseTemporalValue(value)
  if (!date) return value ?? ''

  const month = padDatePart(date.getMonth() + 1)
  const day = padDatePart(date.getDate())
  const hour = padDatePart(date.getHours())
  const minute = padDatePart(date.getMinutes())
  const second = padDatePart(date.getSeconds())

  if (mode === 'tooltip') {
    return `${date.getFullYear()}-${month}-${day} ${hour}:${minute}:${second}`
  }
  if (options.showDate) {
    return `${month}-${day} ${hour}:${minute}`
  }
  return `${hour}:${minute}:${second}`
}

function sanitizeChartSpec(chartSpec) {
  if (!chartSpec || typeof chartSpec !== 'object') return null
  const normalized = JSON.parse(JSON.stringify(chartSpec))
  const backendManaged = normalized.__layout_managed_by_backend === true
  delete normalized.__layout_managed_by_backend

  normalized.animation = false
  normalized.animationDuration = 0
  normalized.animationDurationUpdate = 0
  normalized.animationDelay = 0
  normalized.animationDelayUpdate = 0
  applyPieLegendSafety(normalized)

  if (backendManaged) {
    applyAxisDisplayRules(normalized)
    applyGridContainLabelSafety(normalized)
    applyTooltipDisplayRules(normalized)
    return normalized
  }

  applyTitleLegendGridRules(normalized)
  applyAxisDisplayRules(normalized)
  applyGridContainLabelSafety(normalized)
  applySeriesDisplayRules(normalized)
  applyTooltipDisplayRules(normalized)
  return normalized
}

function applyTitleLegendGridRules(normalized) {
  const title = Array.isArray(normalized.title) ? normalized.title[0] : normalized.title
  const legend = Array.isArray(normalized.legend) ? normalized.legend[0] : normalized.legend
  const titleItem = title && typeof title === 'object' ? title : {}
  const legendItem = legend && typeof legend === 'object' ? legend : {}
  const titlePresent = Boolean(titleItem.text)
  const legendPresent = Boolean(Object.keys(legendItem).length)

  if (titlePresent) {
    titleItem.top = titleItem.top ?? 8
    titleItem.left = titleItem.left ?? 'center'
    titleItem.textStyle = titleItem.textStyle && typeof titleItem.textStyle === 'object' ? titleItem.textStyle : {}
    titleItem.textStyle.fontSize = titleItem.textStyle.fontSize || 16
    titleItem.textStyle.fontWeight = titleItem.textStyle.fontWeight || 600
  }

  if (legendPresent) {
    legendItem.type = legendItem.type || 'scroll'
    legendItem.left = legendItem.left ?? 'center'
    legendItem.top = legendItem.top ?? (titlePresent ? 36 : 8)
    legendItem.itemWidth = legendItem.itemWidth || 14
    legendItem.itemHeight = legendItem.itemHeight || 10
    legendItem.textStyle = legendItem.textStyle && typeof legendItem.textStyle === 'object' ? legendItem.textStyle : {}
    legendItem.textStyle.fontSize = legendItem.textStyle.fontSize || 11
  }

  if (Array.isArray(normalized.title)) {
    normalized.title[0] = titleItem
  } else if (titlePresent) {
    normalized.title = titleItem
  }

  if (Array.isArray(normalized.legend)) {
    normalized.legend[0] = legendItem
  } else if (legendPresent) {
    normalized.legend = legendItem
  }

  const currentGrid = Array.isArray(normalized.grid) ? normalized.grid[0] : normalized.grid
  const gridItem = currentGrid && typeof currentGrid === 'object' ? currentGrid : {}
  let topOffset = 56
  if (titlePresent && legendPresent) topOffset = 96
  else if (titlePresent) topOffset = 64
  else if (legendPresent) topOffset = 72
  gridItem.top = gridItem.top ?? topOffset
  gridItem.left = gridItem.left ?? 56
  gridItem.right = gridItem.right ?? 24
  gridItem.bottom = gridItem.bottom ?? 64
  gridItem.containLabel = gridItem.containLabel ?? true

  if (Array.isArray(normalized.grid)) {
    normalized.grid[0] = gridItem
  } else {
    normalized.grid = gridItem
  }
}

function applyAxisDisplayRules(normalized) {
  ;['xAxis', 'yAxis'].forEach((axisKey) => {
    const axes = normalized[axisKey]
    const axisList = Array.isArray(axes) ? axes : (axes && typeof axes === 'object' ? [axes] : [])
    axisList.forEach((axis) => {
      if (!axis || typeof axis !== 'object') return
      axis.animation = false
      axis.animationDuration = 0
      axis.animationDurationUpdate = 0
      axis.animationDelay = 0
      axis.animationDelayUpdate = 0
      axis.nameGap = axis.nameGap || 18
      axis.axisLabel = axis.axisLabel && typeof axis.axisLabel === 'object' ? axis.axisLabel : {}
      axis.axisLabel.hideOverlap = true
      axis.axisLabel.fontSize = axis.axisLabel.fontSize || 11
      axis.axisLabel.margin = axis.axisLabel.margin || 10
      axis.axisLabel.overflow = axis.axisLabel.overflow || 'truncate'

      if (axis.type === 'value' || axis.type === 'log') {
        axis.axisLabel.formatter = (value) => formatChartNumber(value)
      }

      const categoryData = Array.isArray(axis.data) ? axis.data : []
      const maxLabelLength = categoryData.reduce((max, item) => Math.max(max, String(item ?? '').length), 0)
      if (axis.type === 'category') {
        if (isTemporalCategoryAxis(axis, categoryData)) {
          const showDate = hasMultipleTemporalDays(categoryData)
          axis.axisLabel.formatter = (value) => formatTemporalValue(value, 'axis', { showDate })
          axis.axisLabel.rotate = axis.axisLabel.rotate ?? 0
          axis.axisLabel.width = axis.axisLabel.width || (showDate ? 90 : 72)
          axis.axisLabel.overflow = 'break'
        }
        if (categoryData.length > 8 || maxLabelLength > 8) {
          axis.axisLabel.rotate = axis.axisLabel.rotate ?? (maxLabelLength > 14 ? 45 : 30)
        }
        if (categoryData.length > 16) {
          const intervalStep = Math.max(Math.ceil(categoryData.length / 8) - 1, 0)
          axis.axisLabel.interval = axis.axisLabel.interval ?? intervalStep
        }
        if (maxLabelLength > 12) {
          axis.axisLabel.width = axis.axisLabel.width || 84
        }
      }
    })
  })
}

function coerceAxisList(value) {
  if (Array.isArray(value)) return value.filter((item) => item && typeof item === 'object')
  return value && typeof value === 'object' ? [value] : []
}

function getSeriesNumericValue(rawValue) {
  const value = rawValue && typeof rawValue === 'object' && !Array.isArray(rawValue) && 'value' in rawValue
    ? rawValue.value
    : rawValue
  const numericValue = Array.isArray(value) ? value[value.length - 1] : value
  const parsed = typeof numericValue === 'number' ? numericValue : Number(numericValue)
  return Number.isFinite(parsed) ? parsed : null
}

function estimateAxisLabelWidth(label) {
  const text = String(label ?? '')
  if (!text) return 0
  let width = 0
  for (const char of text) {
    width += /[\u4e00-\u9fff]/.test(char) ? 12 : 7
  }
  return width
}

function ensureGridOffset(value, minValue) {
  if (typeof value === 'number') return Math.max(value, minValue)
  if (typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value.trim())) {
    return Math.max(Number(value), minValue)
  }
  return value ?? minValue
}

function applyGridContainLabelSafety(normalized) {
  const currentGrid = Array.isArray(normalized.grid) ? normalized.grid[0] : normalized.grid
  const gridItem = currentGrid && typeof currentGrid === 'object' ? currentGrid : {}
  const yAxes = coerceAxisList(normalized.yAxis)
  const seriesList = Array.isArray(normalized.series) ? normalized.series : []

  let minLeft = 56
  let minRight = 24

  yAxes.forEach((axis, axisIndex) => {
    const position = axis.position === 'right' ? 'right' : 'left'
    let labelWidth = 0

    if (axis.type === 'category') {
      const labels = Array.isArray(axis.data) ? axis.data : []
      labelWidth = labels.reduce((max, item) => Math.max(max, estimateAxisLabelWidth(item)), 0)
    } else if (axis.type === 'value' || axis.type === 'log' || !axis.type) {
      const values = []
      seriesList.forEach((series) => {
        if (!series || typeof series !== 'object') return
        if ((series.yAxisIndex ?? 0) !== axisIndex) return
        const data = Array.isArray(series.data) ? series.data : []
        data.forEach((item) => {
          const numericValue = getSeriesNumericValue(item)
          if (numericValue !== null) values.push(numericValue)
        })
      })
      if (values.length) {
        const minValue = Math.min(0, ...values)
        const maxValue = Math.max(...values)
        labelWidth = [minValue, maxValue]
          .map((value) => estimateAxisLabelWidth(formatChartNumber(value)))
          .reduce((max, width) => Math.max(max, width), 0)
      }
    }

    if (labelWidth <= 0) return
    const safeOffset = Math.min(132, Math.max(56, Math.ceil(labelWidth + 24)))
    if (position === 'right') {
      minRight = Math.max(minRight, safeOffset)
    } else {
      minLeft = Math.max(minLeft, safeOffset)
    }
  })

  gridItem.left = ensureGridOffset(gridItem.left, minLeft)
  gridItem.right = ensureGridOffset(gridItem.right, minRight)
  gridItem.containLabel = gridItem.containLabel ?? true

  if (Array.isArray(normalized.grid)) {
    normalized.grid[0] = gridItem
  } else {
    normalized.grid = gridItem
  }
}

function applySeriesDisplayRules(normalized) {
  if (!Array.isArray(normalized.series)) return
  normalized.series.forEach((series) => {
    if (!series || typeof series !== 'object') return
    series.animation = false
    series.label = series.label && typeof series.label === 'object' ? series.label : {}
    series.label.fontSize = series.label.fontSize || 11
    series.label.overflow = series.label.overflow || 'truncate'
    series.labelLayout = series.labelLayout && typeof series.labelLayout === 'object' ? series.labelLayout : {}
    series.labelLayout.hideOverlap = true

    if (series.type === 'line' && series.lineStyle && typeof series.lineStyle === 'object' && series.lineStyle.show === false) {
      delete series.lineStyle.show
      if (Object.keys(series.lineStyle).length === 0) {
        delete series.lineStyle
      }
    }

    if (series.label.show) {
      const originalFormatter = series.label.formatter
      series.label.formatter = (params) => {
        if (typeof originalFormatter === 'function') return originalFormatter(params)
        const rawValue = Array.isArray(params?.value) ? params.value[params.value.length - 1] : params?.value
        return formatChartNumber(rawValue)
      }
    }

    if (series.type === 'bar') {
      series.barMinHeight = Math.max(series.barMinHeight || 0, 6)
    }

    if (series.type === 'pie') {
      series.avoidLabelOverlap = true
      series.minAngle = Math.max(series.minAngle || 0, 3)
      series.percentPrecision = series.percentPrecision || 2
      if (series.label.show !== false) {
        const originalFormatter = series.label.formatter
        series.label.formatter = (params) => {
          if (typeof originalFormatter === 'function') return originalFormatter(params)
          return `${params?.name || ''}\n${formatChartNumber(params?.value)} (${(params?.percent ?? 0).toFixed(2)}%)`
        }
      }
    }
  })
}

function applyTooltipDisplayRules(normalized) {
  normalized.tooltip = normalized.tooltip && typeof normalized.tooltip === 'object' ? normalized.tooltip : {}
  if (typeof normalized.tooltip.formatter !== 'function') {
    normalized.tooltip.formatter = (params) => {
      const rows = Array.isArray(params) ? params : [params]
      const axisValue = rows.find(Boolean)?.axisValue ?? rows.find(Boolean)?.name
      const axisHeader = isTemporalValue(axisValue)
        ? `<div style="margin-bottom:4px;font-weight:600;">${formatTemporalValue(axisValue, 'tooltip')}</div>`
        : (axisValue !== undefined && axisValue !== null && axisValue !== ''
          ? `<div style="margin-bottom:4px;font-weight:600;">${escapeTooltipText(axisValue)}</div>`
          : '')
      const body = rows
        .filter(Boolean)
        .map((item) => {
          const marker = item.marker || ''
          const seriesName = escapeTooltipText(item.seriesName || item.name || '')
          const rawValue = Array.isArray(item.value) ? item.value[item.value.length - 1] : item.value
          return `${marker}${seriesName}: ${escapeTooltipText(formatChartNumber(rawValue))}`
        })
        .join('<br/>')
      return `${axisHeader}${body}`
    }
  }
}

const estimateChartHeight = (chartSpec) => {
  if (!chartSpec || typeof chartSpec !== 'object') return 320

  let height = 360
  const title = Array.isArray(chartSpec.title) ? chartSpec.title[0] : chartSpec.title
  const legend = Array.isArray(chartSpec.legend) ? chartSpec.legend[0] : chartSpec.legend
  const xAxis = Array.isArray(chartSpec.xAxis) ? chartSpec.xAxis[0] : chartSpec.xAxis
  const seriesList = Array.isArray(chartSpec.series) ? chartSpec.series : []

  if (title?.text) {
    height += 24
  }

  const legendData = Array.isArray(legend?.data) ? legend.data : []
  if (legend) {
    height += legendData.length > 6 ? 56 : 32
  }

  const categoryData = Array.isArray(xAxis?.data) ? xAxis.data : []
  const maxLabelLength = categoryData.reduce((max, item) => Math.max(max, String(item ?? '').length), 0)
  if (categoryData.length > 8 || maxLabelLength > 8) {
    height += 48
  }

  if (seriesList.some((series) => series?.type === 'pie')) {
    height = Math.max(height, legendData.length > 5 ? 460 : 400)
  }

  return Math.min(Math.max(height, 320), 560)
}

const renderSpecChart = async () => {
  await nextTick()
  if (!isSpecMode.value || !specContainer.value) return

  if (!chartInstance.value) {
    chartInstance.value = echarts.init(specContainer.value, null, { renderer: 'canvas' })
  }
  chartInstance.value.setOption(normalizedChartSpec.value, true)
  chartInstance.value.off('click')
  chartInstance.value.on('click', (params) => {
    emit('point-click', params)
  })
  chartInstance.value.resize()
}

watch(
  () => props.chartSpec,
  async () => {
    if (isSpecMode.value) {
      await renderSpecChart()
      return
    }

    if (chartInstance.value) {
      chartInstance.value.dispose()
      chartInstance.value = null
    }
  },
  { deep: true }
)

onMounted(async () => {
  if (isSpecMode.value) {
    await renderSpecChart()
  }
})

const getChartDataUrl = async () => {
  if (isSpecMode.value && chartInstance.value?.getDataURL) {
    return chartInstance.value.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: '#ffffff'
    })
  }
  throw new Error('当前图表缺少结构化配置，无法导出图片')
}

const downloadChartImage = async (filename = 'analysis-chart.png') => {
  const dataUrl = await getChartDataUrl()
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
}

onBeforeUnmount(() => {
  if (chartInstance.value) {
    chartInstance.value.dispose()
    chartInstance.value = null
  }
})

defineExpose({
  getChartDataUrl,
  downloadChartImage
})
</script>

<style scoped>
.chart-display {
  min-height: 280px;
  border: 1px solid #dbe3ef;
  border-radius: 18px;
  overflow: hidden;
  background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
}

.chart-canvas,
.chart-display {
  position: relative;
  width: 100%;
  min-height: 320px;
  background: #ffffff;
}

.chart-canvas {
  min-height: 320px;
}

.chart-placeholder {
  min-height: 280px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #94a3b8;
}

.placeholder-icon {
  width: 56px;
  height: 56px;
  border-radius: 18px;
  background: #e0edff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
}
</style>
