<template>
  <section class="data-panel">
    <div class="data-panel-content">
      <div class="search-section">
        <div class="search-box">
          <span class="search-icon">⌕</span>
          <input
            v-model="searchKeyword"
            class="search-input"
            type="text"
            placeholder="从数据资源中搜索节点、表或文件..."
          >
        </div>
      </div>

      <div class="upload-section">
        <div class="upload-buttons">
          <button
            v-for="mode in uploadModes"
            :key="mode.value"
            class="upload-box"
            :class="{ active: activeUploadMode === mode.value, [mode.value]: true }"
            type="button"
            @click="activeUploadMode = mode.value"
          >
            <span class="upload-box-icon">{{ mode.icon }}</span>
            <span>{{ mode.label }}</span>
          </button>
        </div>
      </div>

      <div v-if="activeUploadMode === 'uns'" class="uns-nodes-section">
        <div class="section-header">
          <span class="section-title">🔗 数据节点结构</span>
          <div class="section-meta">已选 {{ selectedUnsNodes.length }} 项</div>
        </div>

        <div class="uns-tree">
          <div v-for="group in filteredUnsGroups" :key="group.id" class="tree-node">
            <div class="tree-node-content root">
              <span class="tree-node-icon">{{ group.icon }}</span>
              <span class="node-name">{{ group.name }}</span>
            </div>

            <div class="tree-children">
              <button
                v-for="node in group.children"
                :key="node.id"
                class="resource-chip"
                :class="{ active: selectedUnsNodes.includes(node.id) }"
                type="button"
                @click="toggleUnsNode(node)"
              >
                <span class="resource-chip-icon">{{ node.icon }}</span>
                <span>{{ node.name }}</span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="activeUploadMode === 'knowledge'" class="knowledge-section">
        <div class="section-header">
          <span class="section-title">📚 已关联知识资源</span>
          <div class="section-meta">后续按统一知识库接口接入</div>
        </div>

        <div class="placeholder-card">
          <div class="placeholder-title">知识库入口已预留</div>
          <div class="placeholder-text">
            当前先保留原型中的知识资源区域，后续再和统一知识库项目对接。
          </div>
        </div>
      </div>

      <div v-else class="external-section">
        <div class="section-header">
          <span class="section-title">📎 手动指定外部数据源</span>
          <button class="plain-action" type="button" @click="refreshDataSource">应用</button>
        </div>

        <div class="form-grid">
          <label class="form-group">
            <span class="form-label">本地文件路径</span>
            <input v-model="localFilePath" class="form-input" placeholder="例如 D:/data/sales.csv">
          </label>

          <label class="form-group">
            <span class="form-label">数据库表</span>
            <select v-model="selectedTable" class="form-input">
              <option value="">请选择数据表</option>
              <option v-for="table in databaseTables" :key="table" :value="table">{{ table }}</option>
            </select>
          </label>

          <label class="form-group full-width">
            <span class="form-label">API 地址</span>
            <input v-model="apiEndpoint" class="form-input" placeholder="https://example.com/api/data">
          </label>
        </div>
      </div>

      <div class="imported-data-section">
        <div class="section-header">
          <div class="data-tabs">
            <button
              class="data-tab"
              :class="{ active: importedTab === 'imported' }"
              type="button"
              @click="importedTab = 'imported'"
            >
              📊 已导入的数据
            </button>
            <button
              class="data-tab"
              :class="{ active: importedTab === 'knowledge' }"
              type="button"
              @click="importedTab = 'knowledge'"
            >
              📚 当前会话资源
            </button>
          </div>
        </div>

        <div v-if="importedTab === 'imported'" class="imported-list">
          <template v-if="resourceCards.length">
            <button
              v-for="resource in resourceCards"
              :key="resource.id"
              class="data-item"
              :class="{ active: selectedResourceId === resource.id }"
              type="button"
              @click="selectResource(resource)"
            >
              <div class="data-item-icon">{{ resource.icon }}</div>
              <div class="data-item-body">
                <div class="data-item-title">{{ resource.title }}</div>
                <div class="data-item-subtitle">{{ resource.description }}</div>
              </div>
            </button>
          </template>
          <div v-else class="empty-state">
            <div class="empty-state-icon">☁</div>
            <p>暂无手动指定数据源</p>
            <span>可通过上方切换到“上传外部数据”进行补充。</span>
          </div>
        </div>

        <div v-else class="imported-list">
          <div class="context-card">
            <div class="context-card-title">当前洞察空间</div>
            <div class="context-card-value">{{ activeSpaceName || '未选择空间' }}</div>
          </div>
          <div class="context-card">
            <div class="context-card-title">当前会话</div>
            <div class="context-card-value">{{ activeConversation?.title || '会话已创建，等待提问' }}</div>
          </div>
          <div class="context-card">
            <div class="context-card-title">当前前端选择</div>
            <div class="context-card-value">{{ selectedDataSourceLabel }}</div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  activeSpaceName: { type: String, default: '' },
  activeConversation: { type: Object, default: null },
  selectedDataSource: { type: Object, default: null }
})

const emit = defineEmits(['data-source-change'])

const searchKeyword = ref('')
const activeUploadMode = ref('uns')
const importedTab = ref('imported')
const localFilePath = ref('')
const selectedTable = ref('')
const apiEndpoint = ref('')
const selectedUnsNodes = ref([])
const selectedResourceId = ref('')

const uploadModes = [
  { value: 'uns', label: '关联节点资源', icon: '🔗' },
  { value: 'knowledge', label: '关联知识库', icon: '📚' },
  { value: 'external', label: '上传外部数据', icon: '📎' }
]

const databaseTables = [
  'alarm_record',
  'alarm_treatment',
  'sales_order',
  'production_daily'
]

const unsGroups = [
  {
    id: 'group-sales',
    name: '经营分析',
    icon: '📈',
    children: [
      { id: 'sales-q4', name: '销售记录文件', icon: '📄', type: 'local_file', value: 'sales_record_file' },
      { id: 'sales-table', name: '销售订单表', icon: '🗂', type: 'table', value: 'sales_order' }
    ]
  },
  {
    id: 'group-alarm',
    name: '报警中心',
    icon: '🚨',
    children: [
      { id: 'alarm-record', name: '报警记录表', icon: '📋', type: 'table', value: 'alarm_record' },
      { id: 'alarm-treatment', name: '报警处置表', icon: '🛠', type: 'table', value: 'alarm_treatment' }
    ]
  },
  {
    id: 'group-api',
    name: '接口资源',
    icon: '🌐',
    children: [
      { id: 'alarm-api', name: '报警汇总 API', icon: '🔌', type: 'api', value: 'https://example.com/api/alarm' }
    ]
  }
]

const filteredUnsGroups = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  if (!keyword) return unsGroups
  return unsGroups
    .map((group) => ({
      ...group,
      children: group.children.filter((item) => item.name.toLowerCase().includes(keyword))
    }))
    .filter((group) => group.children.length)
})

const resourceCards = computed(() => {
  const cards = []
  if (localFilePath.value.trim()) {
    cards.push({
      id: 'manual-local-file',
      icon: '📄',
      title: '本地文件',
      description: localFilePath.value.trim(),
      payload: { type: 'local_file', value: localFilePath.value.trim() }
    })
  }
  if (selectedTable.value) {
    cards.push({
      id: 'manual-table',
      icon: '🗂',
      title: '数据库表',
      description: selectedTable.value,
      payload: { type: 'table', value: selectedTable.value }
    })
  }
  if (apiEndpoint.value.trim()) {
    cards.push({
      id: 'manual-api',
      icon: '🔌',
      title: 'API 数据源',
      description: apiEndpoint.value.trim(),
      payload: { type: 'api', value: apiEndpoint.value.trim() }
    })
  }
  selectedUnsNodes.value.forEach((nodeId) => {
    const node = unsGroups.flatMap((group) => group.children).find((item) => item.id === nodeId)
    if (!node) return
    cards.push({
      id: node.id,
      icon: node.icon,
      title: node.name,
      description: node.type,
      payload: { type: node.type, value: node.value }
    })
  })
  return cards
})

const selectedDataSourceLabel = computed(() => {
  if (!props.selectedDataSource) return '尚未指定'
  const type = props.selectedDataSource.type || 'unknown'
  const value = props.selectedDataSource.value || ''
  return `${type}${value ? ` · ${value}` : ''}`
})

const refreshDataSource = () => {
  if (localFilePath.value.trim()) {
    selectedResourceId.value = 'manual-local-file'
    emit('data-source-change', { type: 'local_file', value: localFilePath.value.trim() })
    return
  }
  if (selectedTable.value) {
    selectedResourceId.value = 'manual-table'
    emit('data-source-change', { type: 'table', value: selectedTable.value })
    return
  }
  if (apiEndpoint.value.trim()) {
    selectedResourceId.value = 'manual-api'
    emit('data-source-change', { type: 'api', value: apiEndpoint.value.trim() })
  }
}

const toggleUnsNode = (node) => {
  if (selectedUnsNodes.value.includes(node.id)) {
    selectedUnsNodes.value = selectedUnsNodes.value.filter((item) => item !== node.id)
    if (selectedResourceId.value === node.id) {
      selectedResourceId.value = ''
      emit('data-source-change', null)
    }
    return
  }
  selectedUnsNodes.value = [...selectedUnsNodes.value, node.id]
  selectedResourceId.value = node.id
  emit('data-source-change', { type: node.type, value: node.value })
}

const selectResource = (resource) => {
  selectedResourceId.value = resource.id
  emit('data-source-change', resource.payload)
}
</script>

<style scoped>
.data-panel {
  width: 360px;
  min-width: 360px;
  background: #ffffff;
  border-right: 1px solid #dbe3ef;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.data-panel-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.search-section,
.upload-section,
.uns-nodes-section,
.knowledge-section,
.external-section,
.imported-data-section {
  padding: 16px;
}

.search-section,
.upload-section,
.uns-nodes-section,
.knowledge-section,
.external-section {
  border-bottom: 1px solid #edf2f7;
}

.search-box {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid #dbe3ef;
  border-radius: 14px;
  background: #f8fbff;
}

.search-icon {
  color: #64748b;
}

.search-input,
.form-input {
  width: 100%;
  border: none;
  outline: none;
  background: transparent;
  color: #0f172a;
  font-size: 14px;
  font-family: inherit;
}

.upload-buttons {
  display: flex;
  gap: 10px;
}

.upload-box {
  flex: 1;
  border: 1px solid #dbe3ef;
  border-radius: 14px;
  background: #f8fbff;
  padding: 12px 10px;
  cursor: pointer;
  color: #475569;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  transition: all 0.2s ease;
}

.upload-box:hover,
.upload-box.active {
  border-color: #60a5fa;
  background: rgba(37, 99, 235, 0.08);
  color: #1d4ed8;
}

.upload-box-icon {
  font-size: 18px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-title {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
}

.section-meta {
  font-size: 12px;
  color: #64748b;
}

.plain-action {
  border: none;
  background: #e0edff;
  color: #1d4ed8;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.uns-nodes-section,
.knowledge-section,
.external-section,
.imported-data-section {
  flex-shrink: 0;
}

.uns-nodes-section {
  max-height: 300px;
  overflow-y: auto;
}

.uns-tree {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tree-node-content.root {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.tree-node-icon {
  width: 30px;
  height: 30px;
  border-radius: 10px;
  background: #eff6ff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.node-name {
  font-size: 13px;
  font-weight: 700;
  color: #334155;
}

.tree-children {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.resource-chip {
  border: 1px solid #dbe3ef;
  background: #fff;
  border-radius: 999px;
  padding: 8px 12px;
  color: #475569;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}

.resource-chip.active,
.resource-chip:hover {
  border-color: #60a5fa;
  background: rgba(37, 99, 235, 0.08);
  color: #1d4ed8;
}

.resource-chip-icon {
  font-size: 14px;
}

.placeholder-card,
.context-card {
  background: #f8fbff;
  border: 1px solid #e5edf7;
  border-radius: 16px;
  padding: 14px 16px;
}

.placeholder-title,
.context-card-title {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
}

.placeholder-text,
.context-card-value {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: #64748b;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.form-group.full-width {
  grid-column: 1 / -1;
}

.form-label {
  font-size: 12px;
  color: #64748b;
}

.form-input {
  border: 1px solid #dbe3ef;
  border-radius: 12px;
  padding: 10px 12px;
  background: #fff;
}

.imported-data-section {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.data-tabs {
  display: flex;
  gap: 8px;
}

.data-tab {
  border: none;
  background: transparent;
  border-radius: 999px;
  padding: 8px 12px;
  font-size: 12px;
  color: #64748b;
  cursor: pointer;
}

.data-tab.active,
.data-tab:hover {
  background: #edf4ff;
  color: #1d4ed8;
  font-weight: 600;
}

.imported-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.data-item {
  border: 1px solid #e5edf7;
  background: #fff;
  border-radius: 14px;
  padding: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  cursor: pointer;
  text-align: left;
  transition: border-color 0.2s ease, background 0.2s ease, transform 0.2s ease;
}

.data-item.active,
.data-item:hover {
  border-color: #60a5fa;
  background: rgba(37, 99, 235, 0.05);
}

.data-item-icon {
  width: 38px;
  height: 38px;
  border-radius: 12px;
  background: #eff6ff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}

.data-item-body {
  min-width: 0;
}

.data-item-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}

.data-item-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: #64748b;
  word-break: break-all;
}

.empty-state {
  flex: 1;
  min-height: 160px;
  border: 1px dashed #dbe3ef;
  border-radius: 16px;
  background: #f8fbff;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #64748b;
  text-align: center;
  padding: 20px;
}

.empty-state-icon {
  width: 48px;
  height: 48px;
  border-radius: 16px;
  background: #e0edff;
  color: #2563eb;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  margin-bottom: 12px;
}
</style>
