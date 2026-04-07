<template>
  <div class="chart-display">
    <div v-if="chartUrl" class="chart-container">
      <iframe
        ref="chartIframe"
        :src="iframeSrc"
        class="chart-iframe"
        frameborder="0"
        @load="onIframeLoad"
        @error="onIframeError"
      />

      <div v-if="loading" class="chart-overlay">图表加载中...</div>
      <div v-else-if="error" class="chart-overlay error">{{ error }}</div>
    </div>

    <div v-else class="chart-placeholder">
      <span class="placeholder-icon">📈</span>
      <p>分析图表将在这里展示</p>
    </div>
  </div>
</template>

<script setup>
import { computed, defineExpose, ref, watch } from 'vue'

const props = defineProps({
  chartUrl: {
    type: String,
    default: ''
  }
})

const chartIframe = ref(null)
const loading = ref(Boolean(props.chartUrl))
const error = ref('')
const cacheKey = ref(Date.now())

const iframeSrc = computed(() => {
  if (!props.chartUrl) return ''
  const separator = props.chartUrl.includes('?') ? '&' : '?'
  return `${props.chartUrl}${separator}t=${cacheKey.value}`
})

watch(
  () => props.chartUrl,
  (value) => {
    loading.value = Boolean(value)
    error.value = ''
    cacheKey.value = Date.now()
  },
  { immediate: true }
)

const onIframeLoad = () => {
  loading.value = false
}

const onIframeError = () => {
  loading.value = false
  error.value = '图表加载失败，请稍后重试。'
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const getChartDataUrl = async () => {
  if (!chartIframe.value?.contentWindow || !chartIframe.value?.contentDocument) {
    throw new Error('图表尚未加载完成')
  }

  await wait(120)
  const iframeWindow = chartIframe.value.contentWindow
  const iframeDocument = chartIframe.value.contentDocument
  const chartElement = iframeDocument.querySelector('.chart-container')
  const echarts = iframeWindow.echarts

  if (!chartElement || !echarts?.getInstanceByDom) {
    throw new Error('当前图表暂不支持导出图片')
  }

  const chartInstance = echarts.getInstanceByDom(chartElement)
  if (!chartInstance?.getDataURL) {
    throw new Error('当前图表暂不支持导出图片')
  }

  return chartInstance.getDataURL({
    type: 'png',
    pixelRatio: 2,
    backgroundColor: '#ffffff'
  })
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

.chart-container {
  position: relative;
  min-height: 280px;
}

.chart-iframe {
  width: 100%;
  min-height: 320px;
  border: none;
  display: block;
  background: #ffffff;
}

.chart-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(248, 251, 255, 0.92);
  color: #475569;
  font-size: 14px;
}

.chart-overlay.error {
  color: #dc2626;
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
