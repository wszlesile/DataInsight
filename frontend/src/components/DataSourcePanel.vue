<template>
  <div class="data-source-panel">
    <div class="panel-header">
      <span class="panel-title">数据源</span>
    </div>

    <div class="panel-content">
      <div class="source-section">
        <div class="source-title">本地文件</div>
        <el-input
          v-model="localFilePath"
          placeholder="输入文件路径"
          size="small"
        />
      </div>

      <div class="source-section">
        <div class="source-title">数据库表</div>
        <el-select
          v-model="selectedTable"
          placeholder="选择数据表"
          size="small"
          clearable
        >
          <el-option
            v-for="table in databaseTables"
            :key="table"
            :label="table"
            :value="table"
          />
        </el-select>
      </div>

      <div class="source-section">
        <div class="source-title">API 数据源</div>
        <el-input
          v-model="apiEndpoint"
          placeholder="输入API地址"
          size="small"
        />
      </div>

      <div class="source-actions">
        <el-button type="primary" size="small" @click="refreshData">
          刷新数据
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const localFilePath = ref('')
const selectedTable = ref('')
const apiEndpoint = ref('')

const databaseTables = ref([
  'sales_data',
  'orders',
  'customers',
  'products',
  'alarms'
])

const emit = defineEmits(['data-source-change'])

const refreshData = () => {
  const dataSource = {
    type: selectedTable.value ? 'database' : localFilePath.value ? 'file' : 'api',
    value: selectedTable.value || localFilePath.value || apiEndpoint.value
  }
  emit('data-source-change', dataSource)
}
</script>

<style scoped>
.data-source-panel {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #f5f7fa;
  border-right: 1px solid #e4e7ed;
}

.panel-header {
  padding: 16px;
  flex-shrink: 0;
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.panel-content {
  flex: 1;
  padding: 0 16px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.source-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.source-title {
  font-size: 12px;
  color: #606266;
}

.source-actions {
  margin-top: 8px;
}
</style>