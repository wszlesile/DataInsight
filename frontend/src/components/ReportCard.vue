<template>
  <div class="report-card">
    <div class="card-header">
      <span class="card-title">分析报告</span>
      <el-button text size="small" @click="copyReport">
        复制
      </el-button>
    </div>
    <div class="card-content" v-html="reportContent" />
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  report: {
    type: String,
    default: ''
  }
})

const reportContent = ref(props.report)

const copyReport = () => {
  navigator.clipboard.writeText(props.report).then(() => {
    ElMessage.success('报告已复制')
  })
}
</script>

<style scoped>
.report-card {
  background: #fff;
  border-radius: 8px;
  padding: 16px;
  max-height: 300px;
  overflow-y: auto;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e4e7ed;
}

.card-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.card-content {
  font-size: 14px;
  line-height: 1.8;
  color: #606266;
}
</style>