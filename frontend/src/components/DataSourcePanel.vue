<template>
  <section class="data-panel">
    <div class="data-panel-content">
      <div class="search-section">
        <div class="search-box">
          <span class="search-icon">🔍</span>
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
          <span class="section-title">关联 UNS 节点</span>
          <div class="section-meta">按树浏览节点，支持勾选文件或文件夹后批量导入</div>
        </div>

        <div class="uns-toolbar">
          <span class="uns-selected-text">
            {{ unsSyncing ? 'UNS 选择同步中...' : `已选择 ${selectedUnsNodeIds.length} 个 UNS 节点，勾选后自动生效` }}
          </span>
          <div class="uns-toolbar-actions">
            <button class="plain-action" type="button" @click="reloadUnsTree">
              刷新
            </button>
          </div>
        </div>

        <div class="uns-tree-card">
          <el-tree
            ref="unsTreeRef"
            class="uns-tree"
            node-key="id"
            lazy
            show-checkbox
            :data="unsRootNodes"
            :props="unsTreeProps"
            :indent="18"
            :expand-on-click-node="false"
            :load="loadUnsTreeNode"
            @check="handleUnsCheck"
          >
            <template #default="{ data }">
              <div class="uns-tree-node">
                <span class="uns-tree-icon">{{ data.isFolder ? '📁' : '📄' }}</span>
                <div class="uns-tree-body">
                  <div class="uns-tree-title-row">
                    <div class="uns-tree-title">{{ data.label }}</div>
                    <span class="uns-tree-kind">{{ data.isFolder ? '文件夹' : '文件' }}</span>
                  </div>
                  <div class="uns-tree-meta">
                    <span class="meta-line">别名：{{ data.alias || '-' }}</span>
                    <span v-if="data.pathName" class="meta-line">路径：{{ data.pathName }}</span>
                  </div>
                </div>
              </div>
            </template>
          </el-tree>
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
            当前页面先保留结构和入口，后续再与统一知识资源服务对接。
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
            上传后的文件会保存到当前洞察空间，并转换成空间级数据源定义。当前会话可在下方按需勾选绑定。
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
                      class="edit-btn"
                      type="button"
                      :disabled="editingDatasourceIds.includes(resource.datasource_id)"
                      @click.stop="handleEditDatasourceDescription(resource)"
                    >
                      编辑描述
                    </button>
                    <button
                      class="delete-btn"
                      type="button"
                      :disabled="bindingDatasourceIds.includes(resource.datasource_id) || editingDatasourceIds.includes(resource.datasource_id)"
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
                  <span v-if="resource.sourceValue && resource.sourceValue !== resource.description" class="meta-pill muted source-pill">
                    {{ resource.sourceValue }}
                  </span>
                </div>
              </div>
            </div>
          </template>

          <div v-else class="empty-state">
            <div class="empty-state-icon">☁</div>
            <p>当前空间还没有可用的数据源</p>
            <span>你可以先上传 Excel / CSV 文件，或者从 UNS 节点导入数据源。</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  bindConversationDatasource,
  fetchUnsTreeNodes,
  deleteNamespaceDatasource,
  importNamespaceUnsDatasources,
  listNamespaceUnsSelections,
  listNamespaceDatasources,
  removeNamespaceUnsSelection,
  unbindConversationDatasource,
  updateNamespaceDatasourceDescription,
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
const unsSyncing = ref(false)
const fileInput = ref(null)
const unsTreeRef = ref(null)
const unsRootNodes = ref([])
const selectedUnsNodes = ref([])
const backendSelectedUnsNodeIds = ref([])
const namespaceDatasources = ref([])
const bindingDatasourceIds = ref([])
const editingDatasourceIds = ref([])

let latestDatasourceFetchToken = 0
let latestUnsTreeToken = 0
let unsOperationQueue = Promise.resolve()

const uploadModes = [
  { value: 'uns', label: '关联节点资源', icon: '🌉' },
  { value: 'knowledge', label: '关联知识库', icon: '📎' },
  { value: 'external', label: '上传外部数据', icon: '📛' },
]

const unsTreeProps = {
  label: 'label',
  children: 'children',
  isLeaf: 'isLeaf',
  disabled: 'disabled',
}

const selectedUnsNodeIds = computed(() => selectedUnsNodes.value.map((item) => item.id))

const isUnsFolderNode = (node) => {
  return Boolean(node?.hasChildren) || Number(node?.countChildren || 0) > 0 || Number(node?.type) === 0
}

const mapUnsNode = (node) => {
  const isFolder = isUnsFolderNode(node)
  const countChildren = Number(node.countChildren || 0)
  const hasChildren = Boolean(node.hasChildren)
  const canExpand = isFolder && (hasChildren || countChildren > 0)
  return {
    id: String(node.id || node.alias || ''),
    alias: node.alias || '',
    label: node.name || node.pathName || node.alias || '未命名节点',
    pathName: node.pathName || node.path || '',
    name: node.name || node.pathName || node.alias || '未命名节点',
    path: node.path || node.pathName || '',
    hasChildren,
    countChildren,
    type: Number(node.type ?? -1),
    pathType: Number(node.pathType ?? -1),
    raw: node,
    isFolder,
    disabled: isFolder && !canExpand,
    isLeaf: !canExpand,
    children: [],
  }
}

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
  } catch {
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
  const schema = safeParseJson(item.datasource_schema)
  const filePath = config.file_path || config.table_name || config.endpoint || ''
  const schemaDescription = typeof schema.description === 'string' ? schema.description.trim() : ''
  const icon = item.datasource_type === 'local_file' || item.datasource_type === 'minio_file' ? '📄' : '🗂'
  return {
    id: `datasource-${item.datasource_id}`,
    datasource_id: Number(item.datasource_id),
    checked: Boolean(item.checked),
    title: item.datasource_name,
    description: schemaDescription || filePath || getDatasourceTypeLabel(item.datasource_type),
    sourceValue: filePath,
    icon,
    typeLabel: getDatasourceTypeLabel(item.datasource_type),
    knowledgeTag: item.knowledge_tag || '',
    raw: item,
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

const upsertNamespaceDatasource = (datasource) => {
  if (!datasource) return

  const datasourceId = Number(datasource.datasource_id || datasource.id)
  let replaced = false
  namespaceDatasources.value = namespaceDatasources.value.map((item) => {
    if (Number(item.datasource_id || item.id) !== datasourceId) {
      return item
    }
    replaced = true
    return {
      ...item,
      ...datasource,
      datasource_id: datasourceId,
      id: datasourceId,
      checked: datasource.checked ?? item.checked,
    }
  })

  if (!replaced) {
    namespaceDatasources.value = [
      ...namespaceDatasources.value,
      { ...datasource, datasource_id: datasourceId, id: datasourceId },
    ]
  }
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
    if (fetchToken !== latestDatasourceFetchToken) return
    if (response.data?.success) {
      namespaceDatasources.value = response.data.data || []
    }
  } catch (error) {
    if (fetchToken !== latestDatasourceFetchToken) return
    console.error('List namespace datasources error:', error)
  }
}

const fetchUnsRootNodes = async () => {
  if (!props.activeNamespaceId) {
    unsRootNodes.value = []
    return
  }

  const fetchToken = ++latestUnsTreeToken
  try {
    const response = await fetchUnsTreeNodes(props.activeNamespaceId, '0')
    if (fetchToken !== latestUnsTreeToken) return
    const rows = response.data?.data?.data || []
    unsRootNodes.value = rows.map(mapUnsNode)
    await applyUnsCheckedKeys()
  } catch (error) {
    if (fetchToken !== latestUnsTreeToken) return
    console.error('Fetch UNS root nodes error:', error)
    ElMessage.error(error?.response?.data?.message || '加载 UNS 节点失败')
  }
}

const loadUnsTreeNode = async (node, resolve) => {
  if (node.level === 0) {
    resolve(unsRootNodes.value)
    return
  }

  try {
    const response = await fetchUnsTreeNodes(props.activeNamespaceId, node.data.id)
    const rows = response.data?.data?.data || []
    resolve(rows.map(mapUnsNode))
    await applyUnsCheckedKeys()
  } catch (error) {
    console.error('Load UNS child nodes error:', error)
    ElMessage.error(error?.response?.data?.message || '加载 UNS 子节点失败')
    resolve([])
  }
}

const toUnsNodePayload = (node) => ({
  id: String(node?.id || ''),
  alias: node?.alias || '',
  name: node?.name || node?.label || '',
  path: node?.path || node?.pathName || '',
  pathName: node?.pathName || node?.path || '',
  hasChildren: Boolean(node?.hasChildren),
  countChildren: Number(node?.countChildren || 0),
  type: Number(node?.type ?? -1),
  pathType: Number(node?.pathType ?? -1),
  isFolder: Boolean(node?.isFolder),
})

const handleUnsCheck = (data, checkedState) => {
  const checkedNodes = unsTreeRef.value?.getCheckedNodes(false, false) || []
  selectedUnsNodes.value = checkedNodes
    .filter((item) => item.id)
    .map((item) => toUnsNodePayload(item))

  const checkedKeys = (checkedState?.checkedKeys || []).map((item) => String(item))
  const isChecked = checkedKeys.includes(String(data?.id || ''))
  enqueueUnsSelectionOperation(toUnsNodePayload(data), isChecked)
}

const enqueueUnsSelectionOperation = (node, checked) => {
  if (!node.id) return
  unsOperationQueue = unsOperationQueue
    .catch(() => undefined)
    .then(() => syncUnsSelection(node, checked))
  return unsOperationQueue
}

const syncUnsSelection = async (node, checked) => {
  if (!props.activeNamespaceId) {
    ElMessage.warning('请先创建或选择洞察空间')
    await fetchUnsSelections()
    return
  }
  if (!props.activeConversation?.id) {
    ElMessage.warning('请先创建或选择会话，再选择 UNS 节点')
    await fetchUnsSelections()
    return
  }

  unsSyncing.value = true
  try {
    const response = checked
      ? await importNamespaceUnsDatasources(props.activeNamespaceId, props.activeConversation.id, [node])
      : await removeNamespaceUnsSelection(props.activeNamespaceId, props.activeConversation.id, node.id)
    if (!response.data?.success) {
      throw new Error(response.data?.message || '同步 UNS 节点选择失败')
    }

    await fetchNamespaceDatasources()
    await fetchUnsSelections()
    const failed = response.data?.data?.failed || []
    if (failed.length) {
      ElMessage.warning(`${response.data.message}，部分节点未同步成功`)
    } else {
      ElMessage.success(response.data.message || 'UNS 节点选择已同步')
    }
  } catch (error) {
    console.error('Sync UNS selection error:', error)
    ElMessage.error(error?.response?.data?.message || error.message || '同步 UNS 节点选择失败')
    await fetchUnsSelections()
  } finally {
    unsSyncing.value = false
  }
}

const reloadUnsTree = async () => {
  selectedUnsNodes.value = []
  backendSelectedUnsNodeIds.value = []
  unsRootNodes.value = []
  await fetchUnsSelections()
  await fetchUnsRootNodes()
}

const applyUnsCheckedKeys = async () => {
  await nextTick()
  await new Promise((resolve) => window.setTimeout(resolve, 0))
  if (!unsTreeRef.value) return
  const keys = Array.from(new Set([
    ...backendSelectedUnsNodeIds.value,
    ...selectedUnsNodeIds.value,
  ]))
  unsTreeRef.value.setCheckedKeys(keys, false)
}

const fetchUnsSelections = async () => {
  if (!props.activeNamespaceId || !props.activeConversation?.id) {
    backendSelectedUnsNodeIds.value = []
    return
  }

  try {
    const response = await listNamespaceUnsSelections(props.activeNamespaceId, props.activeConversation.id)
    if (response.data?.success) {
      const rows = response.data.data || []
      const checkedNodeIds = rows
        .map((item) => item.uns_node_id)
        .map((item) => String(item || ''))
        .filter(Boolean)
      backendSelectedUnsNodeIds.value = checkedNodeIds
      selectedUnsNodes.value = checkedNodeIds.map((id) => ({ id }))
      await applyUnsCheckedKeys()
    }
  } catch (error) {
    console.error('Fetch UNS selections error:', error)
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
  } catch {
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

const handleEditDatasourceDescription = async (resource) => {
  if (!props.activeNamespaceId) return

  const datasourceId = Number(resource.datasource_id)
  if (editingDatasourceIds.value.includes(datasourceId)) return

  try {
    const { value } = await ElMessageBox.prompt(
      `请输入“${resource.title}”的数据源描述`,
      '编辑数据源描述',
      {
        inputType: 'textarea',
        inputValue: safeParseJson(resource.raw?.datasource_schema).description || '',
        inputPlaceholder: '例如：2026年报警记录明细表，包含报警时间、位号、报警等级等字段',
        confirmButtonText: '保存',
        cancelButtonText: '取消',
      }
    )

    editingDatasourceIds.value = [...editingDatasourceIds.value, datasourceId]
    const response = await updateNamespaceDatasourceDescription(
      props.activeNamespaceId,
      datasourceId,
      value ?? ''
    )
    if (!response.data?.success) {
      throw new Error(response.data?.message || '更新数据源描述失败')
    }

    upsertNamespaceDatasource(response.data.data)
    ElMessage.success(response.data.message || '数据源描述已更新')
    window.setTimeout(() => {
      syncNamespaceDatasourcesInBackground()
    }, 300)
  } catch (error) {
    if (error === 'cancel' || error === 'close' || error?.action === 'cancel' || error?.action === 'close') {
      return
    }
    console.error('Update datasource description error:', error)
    ElMessage.error(error?.response?.data?.message || error.message || '更新数据源描述失败')
  } finally {
    editingDatasourceIds.value = editingDatasourceIds.value.filter((item) => item !== datasourceId)
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
    selectedUnsNodes.value = []
    backendSelectedUnsNodeIds.value = []
    unsRootNodes.value = []
    await fetchNamespaceDatasources()
    if (activeUploadMode.value === 'uns') {
      await fetchUnsSelections()
      await fetchUnsRootNodes()
    }
  },
  { immediate: true }
)

watch(
  () => props.activeConversation?.id,
  async () => {
    await fetchNamespaceDatasources()
    if (activeUploadMode.value === 'uns') {
      await fetchUnsSelections()
      await applyUnsCheckedKeys()
    }
  },
  { immediate: true }
)

watch(
  () => activeUploadMode.value,
  async (mode) => {
    if (mode !== 'uns' || !props.activeNamespaceId) return
    await fetchUnsSelections()
    if (unsRootNodes.value.length === 0) {
      await fetchUnsRootNodes()
    } else {
      await applyUnsCheckedKeys()
    }
  }
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
  text-align: right;
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

.uns-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.uns-selected-text {
  font-size: 12px;
  color: #64748b;
}

.uns-toolbar-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.uns-tree-card {
  border: 1px solid #e5edf7;
  border-radius: 16px;
  background: #f8fbff;
  padding: 10px 12px;
  max-height: 380px;
  overflow: auto;
}

.uns-tree {
  background: transparent;
  color: #0f172a;
}

.uns-tree-node {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  width: 100%;
  padding: 6px 0;
}

.uns-tree-icon {
  width: 20px;
  text-align: center;
  flex-shrink: 0;
  margin-top: 1px;
}

.uns-tree-body {
  min-width: 0;
  flex: 1;
}

.uns-tree-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.uns-tree-title {
  font-size: 13px;
  color: #0f172a;
  font-weight: 700;
  line-height: 1.4;
  word-break: break-word;
}

.uns-tree-kind {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: #e2e8f0;
  color: #475569;
}

.uns-tree-meta {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: #64748b;
}

.meta-line {
  line-height: 1.5;
  word-break: break-all;
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

.edit-btn {
  border: none;
  background: #e0edff;
  color: #1d4ed8;
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.edit-btn:disabled,
.delete-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.source-pill {
  max-width: 100%;
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

:deep(.uns-tree .el-tree-node__content) {
  min-height: 52px;
  border-radius: 10px;
  margin: 2px 0;
  padding-right: 8px;
}

:deep(.uns-tree .el-tree-node__content:hover) {
  background: rgba(37, 99, 235, 0.08);
}

:deep(.uns-tree .el-checkbox) {
  margin-right: 10px;
}

:deep(.uns-tree .el-tree-node__expand-icon) {
  color: #64748b;
}
</style>
