<template>
  <div class="chart-display">
    <div v-if="chartUrl" class="chart-container">
      <div class="chart-header">
        <span>图表展示</span>
        <el-button text size="small" @click="refreshChart">刷新</el-button>
      </div>
      <iframe
        ref="chartIframe"
        :src="chartUrl"
        frameborder="0"
        class="chart-iframe"
        @load="onIframeLoad"
        @error="onIframeError"
      />
      <div v-if="loading" class="chart-loading">
        <el-icon class="is-loading"><Loading /></el-icon>
        加载中...
      </div>
      <div v-if="error" class="chart-error">
        图表加载失败: {{ error }}
      </div>
    </div>
    <div v-else class="chart-placeholder">
      <div class="placeholder-content">
        <span class="placeholder-icon">📈</span>
        <p>图表将在这里展示</p>
        <p class="debug-url" v-if="debugUrl">URL: {{ debugUrl }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { Loading } from '@element-plus/icons-vue'

const props = defineProps({
  chartUrl: {
    type: String,
    default: ''
  }
})

const chartIframe = ref(null)
const loading = ref(false)
const error = ref('')
const debugUrl = ref('')

watch(() => props.chartUrl, (newUrl) => {
  debugUrl.value = newUrl
  if (newUrl) {
    loading.value = true
    error.value = ''
  }
})

onMounted(() => {
  debugUrl.value = props.chartUrl
})

const onIframeLoad = () => {
  loading.value = false
  console.log('Chart iframe loaded:', props.chartUrl)
}

const onIframeError = (e) => {
  loading.value = false
  error.value = 'iframe加载错误'
  console.error('Chart iframe error:', e)
}

const refreshChart = () => {
  if (chartIframe.value) {
    loading.value = true
    error.value = ''
    chartIframe.value.src = props.chartUrl + '?t=' + new Date().getTime()
  }
}
</script>

<style scoped>
.chart-display {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  min-height: 400px;
}

.chart-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  position: relative;
}

.chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background: #f5f7fa;
  border-bottom: 1px solid #e4e7ed;
  font-size: 14px;
  color: #606266;
}

.chart-iframe {
  flex: 1;
  width: 100%;
  min-height: 400px;
  border: none;
}

.chart-placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
  background: #fafafa;
}

.placeholder-content {
  text-align: center;
  color: #909399;
}

.placeholder-icon {
  font-size: 48px;
  display: block;
  margin-bottom: 16px;
}

.debug-url {
  font-size: 12px;
  color: #c0c4cc;
  margin-top: 8px;
  word-break: break-all;
}

.chart-loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  align-items: center;
  gap: 8px;
  color: #409eff;
}

.chart-error {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #f56c6c;
  text-align: center;
}
</style>