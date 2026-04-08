<template>
  <section class="data-panel">
    <div class="data-panel-content">
      <div class="search-section">
        <div class="search-box">
          <span class="search-icon">🔎</span>
          <input
            v-model="searchKeyword"
            class="search-input"
            type="text"
            placeholder="搜索数据源名称或类型"
          >
        </div>
      </div>

      <div class="upload-section">
        <div class="upload-buttons">
          <button
            v-for="mode in uploadModes"
            :key="mode.value"
            class="upload-box"
            :class="{ active: activeUploadMode === mode.value }"
            type="button"
            @click="activeUploadMode = mode.value"
          >
            <span class="upload-box-icon">{{ mode.icon }}</span>
            <span>{{ mode.label }}</span>
          </button>
        </div>
      </div>

      <div v-if="activeUploadMode === 'uns'" class="placeholder-section">
        <div class="section-header">
          <span class="section-title">关联节点资源</span>
          <div class="section-meta">当前保留原型结构，后续再接真实资源树</div>
        </div>
        <div class="placeholder-card">
          <div class="placeholder-title">UNS 节点资源入口已预留</div>
          <div class="placeholder-text">
            当前版本先聚焦文件数据源上传与会话绑定，节点资源树后续再按统一接口接入。
          </div>
        </div>
      </div>

      <div v-else-if="activeUploadMode === 'knowledge'" class="placeholder-section">
        <div class="section-header">
          <span class="section-title">关联知识库</span>
          <div class="section-meta">知识资源入口已预留</div>
        </div>
        <div class="placeholder-card">
          <div class="placeholder-title">知识库接入稍后统一处理</div>
          <div class="placeholder-text">
            当前页面先保留结构和位置，后续再与统一知识资源服务对接。
          </div>
        </div>
      </div>

      <div v-else class="external-section">
        <div class="section-header">
          <span class="section-title">上传外部数据</span>
          <button
            class="plain-action"
            type="button"
            :disabled="!activeNamespaceId || uploading"
            @click="triggerFileUpload"
          >
            {{ uploading ? '上传中...' : '上传文件' }}
          </button>
        </div>

        <div class="upload-file-card">
          <div class="upload-file-title">上传 Excel / CSV 文件</div>
          <div class="upload-file-text">
            上传后的文件会保存到当前洞察空间，并转换成空间级数据源定义。当前会话可在下方勾选需要绑定的数据源。
          </div>
          <div class="upload-file-tip">
            当前支持：`.csv`、`.xls`、`.xlsx`
          </div>
        </div>

        <input
          ref="fileInput"
          class="hidden-file-input"
          type="file"
          accept=".csv,.xls,.xlsx"
          @change="handleFileChange"
        >
      </div>

      <div class="imported-data-section">
        <div class="section-header">
          <span class="section-title">空间数据源</span>
          <span class="tab-badge">{{ namespaceDatasources.length }}</span>
        </div>

        <div class="imported-list">
          <template v-if="filteredNamespaceDatasources.length">
            <div
              v-for="resource in filteredNamespaceDatasources"
              :key="resource.id"
              class="data-item"
              :class="{ active: resource.checked }"
            >
              <div class="data-item-icon">{{ resource.icon }}</div>
              <div class="data-item-body">
                <div class="data-item-title-row">
                  <div class="data-item-title">{{ resource.title }}</div>
                  <div class="data-item-actions">
                    <label class="bind-checkbox" @click.stop>
                      <input
                        type="checkbox"
                        :checked="resource.checked"
                        :disabled="!activeConversation?.id || bindingDatasourceIds.includes(resource.datasource_id)"
                        @change="toggleDatasourceBinding(resource, $event)"
                      >
                      <span>{{ resource.checked ? '已绑定' : '绑定到当前会话' }}</span>
                    </label>
                    <button
                      class="delete-btn"
                      type="button"
                      :disabled="bindingDatasourceIds.includes(resource.datasource_id)"
                      @click.stop="handleDeleteDatasource(resource)"
                    >
                      删除
                    </button>
                  </div>
                </div>
                <div class="data-item-subtitle">{{ resource.description }}</div>
                <div class="data-item-meta">
                  <span class="meta-pill">{{ resource.typeLabel }}</span>
                  <span v-if="resource.knowledgeTag" class="meta-pill muted">{{ resource.knowledgeTag }}</span>
                </div>
              </div>
            </div>
          </template>

          <div v-else class="empty-state">
            <div class="empty-state-icon">☁</div>
            <p>当前空间还没有可用的数据源</p>
            <span>你可以先上传 Excel 或 CSV 文件，把它们保存为当前空间的数据源。</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  bindConversationDatasource,
  deleteNamespaceDatasource,
  listNamespaceDatasources,
  unbindConversationDatasource,
  uploadNamespaceDatasource,
} from '../api/agent.js'

const props = defineProps({
  activeNamespaceId: { type: [String, Number], default: '' },
  activeSpaceName: { type: String, default: '' },
  activeConversation: { type: Object, default: null },
  selectedDataSource: { type: Object, default: null },
})

const emit = defineEmits(['data-source-change'])

const searchKeyword = ref('')
const activeUploadMode = ref('external')
const uploading = ref(false)
const fileInput = ref(null)
const namespaceDatasources = ref([])
const bindingDatasourceIds = ref([])
let latestDatasourceFetchToken = 0

const uploadModes = [
  { value: 'uns', label: '关联节点资源', icon: '🔆' },
  { value: 'knowledge', label: '关联知识库', icon: '📚' },
  { value: 'external', label: '上传外部数据', icon: '📤' },
]

const filteredNamespaceDatasources = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  return namespaceDatasources.value
    .filter((item) => {
      if (!keyword) return true
      return (
        item.datasource_name?.toLowerCase().includes(keyword) ||
        item.datasource_type?.toLowerCase().includes(keyword)
      )
    })
    .map((item) => mapDatasourceCard(item))
})

const safeParseJson = (value) => {
  if (!value) return {}
  if (typeof value === 'object') return value
  try {
    return JSON.parse(value)
  } catch (error) {
    return {}
  }
}

const getDatasourceTypeLabel = (type) => {
  switch (type) {
    case 'local_file':
      return '本地文件'
    case 'minio_file':
      return 'MinIO 文件'
    case 'table':
      return '数据表'
    case 'api':
      return '接口'
    default:
      return type || '未知类型'
  }
}

const mapDatasourceCard = (item) => {
  const config = safeParseJson(item.datasource_config_json)
  const filePath = config.file_path || config.table_name || config.endpoint || ''
  const icon = item.datasource_type === 'local_file' || item.datasource_type === 'minio_file' ? '📄' : '🗂'
  return {
    id: `datasource-${item.datasource_id}`,
    datasource_id: Number(item.datasource_id),
    checked: Boolean(item.checked),
    title: item.datasource_name,
    description: filePath || getDatasourceTypeLabel(item.datasource_type),
    icon,
    typeLabel: getDatasourceTypeLabel(item.datasource_type),
    knowledgeTag: item.knowledge_tag || '',
    payload: {
      datasourceId: Number(item.datasource_id),
      datasourceName: item.datasource_name,
      type: item.datasource_type,
      value: filePath || item.datasource_name,
    },
  }
}

const setDatasourceChecked = (datasourceId, checked) => {
  namespaceDatasources.value = namespaceDatasources.value.map((item) =>
    Number(item.datasource_id) === Number(datasourceId)
      ? { ...item, checked }
      : item
  )
}

const syncNamespaceDatasourcesInBackground = async () => {
  try {
    await fetchNamespaceDatasources()
  } catch (error) {
    console.error('Sync namespace datasources error:', error)
  }
}

const fetchNamespaceDatasources = async () => {
  if (!props.activeNamespaceId) {
    namespaceDatasources.value = []
    return
  }

  const fetchToken = ++latestDatasourceFetchToken
  try {
    const response = await listNamespaceDatasources(
      props.activeNamespaceId,
      props.activeConversation?.id || undefined
    )
    if (fetchToken !== latestDatasourceFetchToken) {
      return
    }
    if (response.data?.success) {
      namespaceDatasources.value = response.data.data || []
    }
  } catch (error) {
    if (fetchToken !== latestDatasourceFetchToken) {
      return
    }
    console.error('List namespace datasources error:', error)
  }
}

const triggerFileUpload = () => {
  if (!props.activeNamespaceId) {
    ElMessage.warning('请先创建或选择洞察空间，再上传外部数据')
    return
  }
  fileInput.value?.click()
}

const handleFileChange = async (event) => {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || !props.activeNamespaceId) return

  uploading.value = true
  try {
    const response = await uploadNamespaceDatasource(props.activeNamespaceId, file)
    if (!response.data?.success) {
      ElMessage.error(response.data?.message || '上传文件失败')
      return
    }
    await fetchNamespaceDatasources()
    ElMessage.success('文件已上传到当前洞察空间，请按需勾选绑定到会话')
  } catch (error) {
    console.error('Upload datasource file error:', error)
    ElMessage.error(error?.response?.data?.message || '上传文件失败')
  } finally {
    uploading.value = false
  }
}

const handleDeleteDatasource = async (resource) => {
  if (!props.activeNamespaceId) return

  try {
    await ElMessageBox.confirm(
      `确认删除数据源“${resource.title}”吗？`,
      '删除数据源',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
      }
    )
  } catch (error) {
    return
  }

  try {
    const response = await deleteNamespaceDatasource(props.activeNamespaceId, resource.datasource_id)
    if (!response.data?.success) {
      throw new Error(response.data?.message || '删除数据源失败')
    }
    latestDatasourceFetchToken += 1
    namespaceDatasources.value = namespaceDatasources.value.filter(
      (item) => Number(item.datasource_id) !== Number(resource.datasource_id)
    )
    if (props.selectedDataSource?.datasourceId === Number(resource.datasource_id)) {
      emit('data-source-change', null)
    }
    ElMessage.success('数据源已删除')
  } catch (error) {
    ElMessage.error(error?.response?.data?.message || error.message || '删除数据源失败')
  }
}

const toggleDatasourceBinding = async (resource, event) => {
  const checked = event.target.checked
  if (!props.activeConversation?.id) {
    event.target.checked = false
    ElMessage.warning('请先创建会话，再绑定数据源')
    return
  }

  const datasourceId = Number(resource.datasource_id)
  if (bindingDatasourceIds.value.includes(datasourceId)) return
  bindingDatasourceIds.value = [...bindingDatasourceIds.value, datasourceId]

  try {
    if (checked) {
      const response = await bindConversationDatasource(props.activeConversation.id, datasourceId)
      if (!response.data?.success) {
        throw new Error(response.data?.message || '绑定数据源失败')
      }
      latestDatasourceFetchToken += 1
      setDatasourceChecked(datasourceId, true)
      emit('data-source-change', resource.payload)
      ElMessage.success('数据源已绑定到当前会话')
    } else {
      const response = await unbindConversationDatasource(props.activeConversation.id, datasourceId)
      if (!response.data?.success) {
        throw new Error(response.data?.message || '解绑数据源失败')
      }
      latestDatasourceFetchToken += 1
      setDatasourceChecked(datasourceId, false)
      if (props.selectedDataSource?.datasourceId === datasourceId) {
        emit('data-source-change', null)
      }
      ElMessage.success('数据源已从当前会话解绑')
    }
    window.setTimeout(() => {
      syncNamespaceDatasourcesInBackground()
    }, 300)
  } catch (error) {
    console.error('Toggle datasource binding error:', error)
    event.target.checked = !checked
    ElMessage.error(error?.response?.data?.message || error.message || '操作失败')
  } finally {
    bindingDatasourceIds.value = bindingDatasourceIds.value.filter((item) => item !== datasourceId)
  }
}

watch(
  () => props.activeNamespaceId,
  async () => {
    await fetchNamespaceDatasources()
  },
  { immediate: true }
)

watch(
  () => props.activeConversation?.id,
  async () => {
    await fetchNamespaceDatasources()
  },
  { immediate: true }
)
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
.placeholder-section,
.external-section,
.imported-data-section {
  padding: 16px;
}

.search-section,
.upload-section,
.placeholder-section,
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

.search-input {
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

.plain-action:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.placeholder-card {
  background: #f8fbff;
  border: 1px solid #e5edf7;
  border-radius: 16px;
  padding: 14px 16px;
}

.placeholder-title {
  font-size: 13px;
  font-weight: 700;
  color: #1e293b;
}

.placeholder-text {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: #64748b;
}

.upload-file-card {
  border: 1px dashed #c8d8f0;
  border-radius: 16px;
  background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
  padding: 18px 18px 16px;
}

.upload-file-title {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
}

.upload-file-text,
.upload-file-tip {
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: #64748b;
}

.upload-file-tip {
  color: #1d4ed8;
}

.hidden-file-input {
  display: none;
}

.imported-data-section {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: #dbeafe;
  color: #1d4ed8;
  font-size: 11px;
  font-weight: 700;
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
  align-items: flex-start;
  gap: 12px;
  transition: border-color 0.2s ease, background 0.2s ease;
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
  flex: 1;
}

.data-item-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.data-item-title {
  font-size: 13px;
  font-weight: 700;
  color: #0f172a;
}

.data-item-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.data-item-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: #64748b;
  word-break: break-all;
}

.data-item-meta {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.meta-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: #e2e8f0;
  color: #334155;
}

.meta-pill.muted {
  background: #f1f5f9;
  color: #64748b;
}

.bind-checkbox {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  color: #475569;
  font-size: 12px;
  user-select: none;
}

.bind-checkbox input {
  width: 14px;
  height: 14px;
}

.delete-btn {
  border: none;
  background: #fee2e2;
  color: #b91c1c;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.delete-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
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
