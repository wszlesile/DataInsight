<template>
  <div class="chart-display">
    <div v-if="isSpecMode" ref="specContainer" class="chart-canvas" />
    <div v-else class="chart-placeholder">
      <span class="placeholder-icon">图</span>
      <p>分析图表将在这里展示</p>
    </div>
  </div>
</template>

<script setup>
import * as echarts from 'echarts'
import { computed, defineExpose, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  chartSpec: {
    type: Object,
    default: () => null
  }
})

const specContainer = ref(null)
const chartInstance = ref(null)
const isSpecMode = computed(() => props.chartSpec && typeof props.chartSpec === 'object' && Object.keys(props.chartSpec).length > 0)

const renderSpecChart = async () => {
  await nextTick()
  if (!isSpecMode.value || !specContainer.value) return

  if (!chartInstance.value) {
    chartInstance.value = echarts.init(specContainer.value, null, { renderer: 'canvas' })
  }
  chartInstance.value.setOption(props.chartSpec, true)
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
  { immediate: true, deep: true }
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
  height: 320px;
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
