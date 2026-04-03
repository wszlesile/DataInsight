<template>
  <div class="app-container">
    <Sidebar
      :spaces="spaces"
      :active-space="activeNamespace"
      :conversations="conversations"
      :collects="collects"
      :active-conversation-id="activeConversationId"
      @select-space="onSelectSpace"
      @select-conversation="onSelectConversation"
      @rename-conversation="onRenameConversation"
      @select-collect="onSelectCollect"
      @new-conversation="onNewConversation"
    />

    <div class="main-content">
      <div class="content-header">
        <div class="tabs">
          <span
            v-for="tab in spaces"
            :key="tab.id"
            class="tab"
            :class="{ active: activeNamespace === tab.id }"
            @click="onSelectSpace(tab)"
          >
            {{ tab.name }}
          </span>
        </div>

        <div class="header-meta">
          <span v-if="activeConversation">
            当前会话：{{ activeConversation.title || `会话 #${activeConversation.id}` }}
          </span>
          <span v-else>新会话草稿</span>
          <el-button v-if="activeConversation" size="small" @click="onRenameConversation(activeConversation)">
            重命名
          </el-button>
          <el-button
            v-if="activeConversation"
            size="small"
            @click="toggleConversationCollect"
          >
            {{ isCollected('conversation', activeConversation.id) ? '取消收藏会话' : '收藏会话' }}
          </el-button>
        </div>
      </div>

      <div class="content-body">
        <div class="left-panel">
          <DataSourcePanel @data-source-change="onDataSourceChange" />
        </div>

        <div class="right-panel">
          <div class="chat-window">
            <div ref="messagesContainer" class="messages">
              <template v-for="(item, index) in chatHistory" :key="`${item.turnId || index}-${index}`">
                <div class="message user">
                  <div class="message-avatar">U</div>
                  <div class="message-content">{{ item.question }}</div>
                </div>

                <div class="message-actions">
                  <el-button size="small" text @click="openTurnDetail(item.turnId)">查看详情</el-button>
                  <el-button
                    size="small"
                    text
                    @click="toggleCollect({
                      collectType: 'turn',
                      targetId: item.turnId,
                      title: item.question,
                      summaryText: item.report,
                      conversationId: activeConversationId,
                      metadata: { turn_id: item.turnId }
                    })"
                  >
                    {{ isCollected('turn', item.turnId) ? '取消收藏' : '收藏本轮' }}
                  </el-button>
                </div>

                <div v-if="item.progressItems?.length" class="message progress-feed">
                  <div class="message-avatar">AI</div>
                  <div class="message-content">
                    <div class="progress-title">系统工作流</div>
                    <div class="progress-list">
                      <div
                        v-for="progress in item.progressItems"
                        :key="progress.id"
                        class="progress-item"
                        :class="progress.level"
                      >
                        {{ progress.message }}
                      </div>
                    </div>
                  </div>
                </div>

                <div v-if="item.chartUrl" class="message-chart">
                  <ChartDisplay :chart-url="item.chartUrl" />
                </div>

                <div v-if="item.report" class="message report">
                  <div class="message-avatar">AI</div>
                  <div class="message-content" v-html="renderMarkdown(item.report)" />
                </div>
              </template>

              <template v-if="currentQuestion">
                <div class="message user">
                  <div class="message-avatar">U</div>
                  <div class="message-content">{{ currentQuestion }}</div>
                </div>

                <div v-if="loading || currentProgressItems.length" class="message progress-feed">
                  <div class="message-avatar">AI</div>
                  <div class="message-content">
                    <div class="progress-title">
                      系统工作流
                      <span v-if="loading" class="progress-subtitle">实时更新中...</span>
                    </div>
                    <div v-if="currentProgressItems.length" class="progress-list">
                      <div
                        v-for="progress in currentProgressItems"
                        :key="progress.id"
                        class="progress-item"
                        :class="progress.level"
                      >
                        {{ progress.message }}
                      </div>
                    </div>
                    <div v-else class="progress-empty">正在准备分析上下文...</div>
                  </div>
                </div>

                <div v-if="currentTurnId && !loading" class="message-actions">
                  <el-button size="small" text @click="openTurnDetail(currentTurnId)">查看详情</el-button>
                  <el-button
                    size="small"
                    text
                    @click="toggleCollect({
                      collectType: 'turn',
                      targetId: currentTurnId,
                      title: currentQuestion,
                      summaryText: currentReport,
                      conversationId: activeConversationId,
                      metadata: { turn_id: currentTurnId }
                    })"
                  >
                    {{ isCollected('turn', currentTurnId) ? '取消收藏' : '收藏本轮' }}
                  </el-button>
                </div>

                <div v-if="currentChartUrl" class="message-chart">
                  <ChartDisplay :chart-url="currentChartUrl" />
                </div>

                <div v-if="currentReport" class="message report">
                  <div class="message-avatar">AI</div>
                  <div class="message-content" v-html="renderMarkdown(currentReport)" />
                </div>
              </template>

              <div v-if="chatHistory.length === 0 && !currentQuestion" class="empty-tip">
                <span class="tip-icon">DI</span>
                <p>输入你的问题，开始一段可持续追问的数据洞察会话。</p>
              </div>
            </div>

            <div class="input-area">
              <ChatInput :loading="loading" @send="onSendMessage" />
            </div>
          </div>
        </div>
      </div>
    </div>

    <el-drawer v-model="turnDetailVisible" title="轮次详情" size="48%" destroy-on-close>
      <div v-if="turnDetailLoading" class="detail-empty">正在加载详情...</div>
      <div v-else-if="turnDetail" class="turn-detail">
        <div class="detail-header">
          <div>
            <div class="detail-title">第 {{ turnDetail.turn.turn_no }} 轮分析</div>
            <div class="detail-meta">
              状态：{{ turnDetail.turn.status }} | 开始时间：{{ formatDateTime(turnDetail.turn.started_at) }}
            </div>
          </div>
          <el-button
            size="small"
            @click="toggleCollect({
              collectType: 'turn',
              targetId: turnDetail.turn.id,
              title: turnDetail.turn.user_query,
              summaryText: turnDetail.turn.final_answer,
              conversationId: turnDetail.conversation.id,
              metadata: { turn_id: turnDetail.turn.id }
            })"
          >
            {{ isCollected('turn', turnDetail.turn.id) ? '取消收藏' : '收藏本轮' }}
          </el-button>
        </div>

        <div class="detail-section">
          <div class="section-title">问题</div>
          <div class="detail-card">{{ turnDetail.turn.user_query }}</div>
        </div>

        <div v-if="turnDetail.turn.final_answer" class="detail-section">
          <div class="section-title">最终结论</div>
          <div class="detail-card" v-html="renderMarkdown(turnDetail.turn.final_answer)" />
        </div>

        <div class="detail-section">
          <div class="section-title">消息明细</div>
          <div class="detail-list">
            <div v-for="message in turnDetail.messages" :key="message.id" class="detail-list-item">
              <div class="detail-list-actions">
                <span class="detail-pill">{{ message.role }}</span>
                <span class="detail-pill muted">{{ message.message_kind }}</span>
              </div>
              <div class="detail-card">{{ message.content || '空内容' }}</div>
            </div>
          </div>
        </div>

        <div v-if="turnDetail.artifacts.length" class="detail-section">
          <div class="section-title">分析产物</div>
          <div class="detail-list">
            <div v-for="artifact in turnDetail.artifacts" :key="artifact.id" class="detail-list-item">
              <div class="detail-list-actions">
                <span class="detail-pill">{{ artifact.artifact_type }}</span>
                <el-button v-if="artifact.file_id" size="small" text @click="previewChartArtifact(artifact)">预览</el-button>
                <el-button v-if="artifact.file_id" size="small" text @click="openArtifactFile(artifact)">打开</el-button>
                <el-button
                  size="small"
                  text
                  @click="toggleCollect({
                    collectType: 'artifact',
                    targetId: artifact.id,
                    title: artifact.title,
                    summaryText: artifact.summary_text,
                    conversationId: turnDetail.conversation.id,
                    artifactId: artifact.id,
                    metadata: { turn_id: turnDetail.turn.id, file_id: artifact.file_id }
                  })"
                >
                  {{ isCollected('artifact', artifact.id) ? '取消收藏' : '收藏产物' }}
                </el-button>
              </div>
              <div class="detail-card">
                <div class="artifact-title">{{ artifact.title || '未命名产物' }}</div>
                <div v-if="artifact.summary_text" class="artifact-summary">{{ artifact.summary_text }}</div>
                <div v-if="artifact.file_id" class="artifact-link">文件：{{ artifact.file_id }}</div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="previewArtifact?.file_id" class="detail-section">
          <div class="section-title">产物预览</div>
          <div class="detail-card preview-card">
            <div class="artifact-title">{{ previewArtifact.title || '图表预览' }}</div>
            <ChartDisplay :chart-url="normalizeFileUrl(previewArtifact.file_id)" />
          </div>
        </div>
      </div>
      <div v-else class="detail-empty">没有可展示的轮次详情。</div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { marked } from 'marked'
import { ElMessage, ElMessageBox } from 'element-plus'

import Sidebar from './components/Sidebar.vue'
import DataSourcePanel from './components/DataSourcePanel.vue'
import ChatInput from './components/ChatInput.vue'
import ChartDisplay from './components/ChartDisplay.vue'
import {
  createCollect,
  getConversationHistory,
  getTurnDetail,
  listCollects,
  listConversations,
  removeCollect,
  renameConversation,
  streamAgent
} from './api/agent.js'

marked.setOptions({ breaks: true, gfm: true })

const spaces = ref([
  { id: '1', name: '销售数据分析' },
  { id: '2', name: '报警数据分析' }
])

const activeNamespace = ref('1')
const conversations = ref([])
const collects = ref([])
const activeConversationId = ref(0)
const loading = ref(false)
const chatHistory = ref([])
const currentQuestion = ref('')
const currentChartUrl = ref('')
const currentReport = ref('')
const currentAssistantMessage = ref('')
const currentDataSource = ref(null)
const currentProgressItems = ref([])
const currentStreamController = ref(null)
const currentTurnId = ref(0)
const messagesContainer = ref(null)
const turnDetailVisible = ref(false)
const turnDetailLoading = ref(false)
const turnDetail = ref(null)
const previewArtifact = ref(null)

const activeConversation = computed(() =>
  conversations.value.find((item) => item.id === activeConversationId.value) || null
)

const renderMarkdown = (content) => content ? marked.parse(content) : ''
const normalizeFileUrl = (fileId) => `/files/${encodeURIComponent(fileId)}`
const collectKey = (collectType, targetId) => `${collectType}:${targetId}`

const createProgressItem = (event) => ({
  id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
  level: event.level || (event.type === 'assistant' ? 'assistant' : 'info'),
  message: event.message
})

const isCollected = (collectType, targetId) => {
  if (!targetId) return false
  return collects.value.some(
    (item) => collectKey(item.collect_type, item.target_id) === collectKey(collectType, targetId)
  )
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

const addProgressItem = (event) => {
  if (!event?.message) return
  const lastItem = currentProgressItems.value[currentProgressItems.value.length - 1]
  if (lastItem?.message === event.message) return
  currentProgressItems.value.push(createProgressItem(event))
  scrollToBottom()
}

const formatDateTime = (value) => {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const resetCurrentConversationState = () => {
  currentQuestion.value = ''
  currentChartUrl.value = ''
  currentReport.value = ''
  currentAssistantMessage.value = ''
  currentProgressItems.value = []
  currentTurnId.value = 0
}

const finalizeCurrentConversation = () => {
  if (!currentQuestion.value) return
  chatHistory.value.push({
    turnId: currentTurnId.value,
    question: currentQuestion.value,
    chartUrl: currentChartUrl.value,
    report: currentReport.value,
    progressItems: [...currentProgressItems.value]
  })
  resetCurrentConversationState()
  scrollToBottom()
}

const stopCurrentStream = () => {
  if (currentStreamController.value) {
    currentStreamController.value.abort()
    currentStreamController.value = null
  }
}

const mapHistoryItem = (item) => ({
  turnId: item.turn_id,
  question: item.question,
  chartUrl: item.file_id ? normalizeFileUrl(item.file_id) : '',
  report: item.report,
  progressItems: []
})

const fetchConversations = async () => {
  try {
    const response = await listConversations(activeNamespace.value)
    if (response.data.success) conversations.value = response.data.data || []
  } catch (error) {
    console.error('List conversations error:', error)
  }
}

const fetchCollects = async () => {
  try {
    const response = await listCollects(activeNamespace.value)
    if (response.data.success) collects.value = response.data.data || []
  } catch (error) {
    console.error('List collects error:', error)
  }
}

const loadConversationHistory = async (conversationId) => {
  try {
    const response = await getConversationHistory(conversationId)
    if (response.data.success) {
      const data = response.data.data
      activeConversationId.value = data.conversation.id
      chatHistory.value = (data.history || []).map(mapHistoryItem)
      resetCurrentConversationState()
      scrollToBottom()
    }
  } catch (error) {
    console.error('Load history error:', error)
    ElMessage.error('加载会话历史失败')
  }
}

const openTurnDetail = async (turnId) => {
  if (!activeConversationId.value || !turnId) return
  turnDetailVisible.value = true
  turnDetailLoading.value = true
  previewArtifact.value = null
  try {
    const response = await getTurnDetail(activeConversationId.value, turnId)
    turnDetail.value = response.data.success ? response.data.data : null
  } catch (error) {
    console.error('Load turn detail error:', error)
    turnDetail.value = null
    ElMessage.error('加载轮次详情失败')
  } finally {
    turnDetailLoading.value = false
  }
}

const toggleCollect = async ({
  collectType,
  targetId,
  title,
  summaryText,
  conversationId,
  artifactId = 0,
  metadata = {}
}) => {
  if (!targetId) return
  try {
    if (isCollected(collectType, targetId)) {
      await removeCollect({ collect_type: collectType, target_id: targetId })
      ElMessage.success('已取消收藏')
    } else {
      await createCollect({
        collect_type: collectType,
        target_id: targetId,
        title: title || '',
        summary_text: summaryText || '',
        insight_namespace_id: Number(activeNamespace.value),
        insight_conversation_id: conversationId || activeConversationId.value || 0,
        insight_artifact_id: artifactId || 0,
        metadata_json: metadata
      })
      ElMessage.success('收藏成功')
    }
    await fetchCollects()
  } catch (error) {
    console.error('Toggle collect error:', error)
    ElMessage.error('收藏操作失败')
  }
}

const toggleConversationCollect = async () => {
  if (!activeConversation.value) return
  await toggleCollect({
    collectType: 'conversation',
    targetId: activeConversation.value.id,
    title: activeConversation.value.title,
    summaryText: activeConversation.value.summary_text,
    conversationId: activeConversation.value.id,
    metadata: { conversation_id: activeConversation.value.id }
  })
}

const onRenameConversation = async (conversation) => {
  if (!conversation?.id) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的会话标题', '重命名会话', {
      inputValue: conversation.title || '',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputPattern: /.*\S.*/,
      inputErrorMessage: '标题不能为空'
    })
    await renameConversation(conversation.id, value)
    await fetchConversations()
    if (activeConversationId.value === conversation.id) {
      await loadConversationHistory(conversation.id)
    }
    ElMessage.success('会话标题已更新')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Rename conversation error:', error)
      ElMessage.error('重命名失败')
    }
  }
}

const previewChartArtifact = (artifact) => {
  if (artifact?.file_id) previewArtifact.value = artifact
}

const openArtifactFile = (artifact) => {
  if (artifact?.file_id) {
    window.open(normalizeFileUrl(artifact.file_id), '_blank', 'noopener')
  }
}

const onSelectSpace = async (space) => {
  stopCurrentStream()
  loading.value = false
  activeNamespace.value = space.id
  activeConversationId.value = 0
  chatHistory.value = []
  resetCurrentConversationState()
  turnDetailVisible.value = false
  turnDetail.value = null
  previewArtifact.value = null
  await Promise.all([fetchConversations(), fetchCollects()])
}

const onSelectConversation = async (conversation) => {
  stopCurrentStream()
  loading.value = false
  await loadConversationHistory(conversation.id)
}

const onSelectCollect = async (collect) => {
  if (collect.insight_conversation_id) {
    await loadConversationHistory(collect.insight_conversation_id)
  }
  if (collect.collect_type === 'turn' && collect.target_id) {
    await openTurnDetail(collect.target_id)
    return
  }
  if (collect.collect_type !== 'artifact') return
  try {
    const metadata = typeof collect.metadata_json === 'string'
      ? JSON.parse(collect.metadata_json || '{}')
      : (collect.metadata_json || {})
    if (metadata.turn_id) {
      await openTurnDetail(metadata.turn_id)
    }
    if (metadata.file_id && turnDetail.value?.artifacts?.length) {
      previewArtifact.value = turnDetail.value.artifacts.find((item) => item.file_id === metadata.file_id) || null
    }
  } catch (error) {
    console.error('Parse collect metadata error:', error)
  }
}

const onNewConversation = () => {
  stopCurrentStream()
  loading.value = false
  activeConversationId.value = 0
  chatHistory.value = []
  resetCurrentConversationState()
  turnDetailVisible.value = false
  turnDetail.value = null
  previewArtifact.value = null
}

const onDataSourceChange = (dataSource) => {
  currentDataSource.value = dataSource
}

const handleStreamEvent = async (event) => {
  if (!event || typeof event !== 'object') return
  if (event.conversation_id) activeConversationId.value = Number(event.conversation_id)
  if (event.turn_id) currentTurnId.value = Number(event.turn_id)

  if (event.type === 'session') {
    await Promise.all([fetchConversations(), fetchCollects()])
    return
  }
  if (event.type === 'result') {
    if (event.file_id) currentChartUrl.value = normalizeFileUrl(event.file_id)
    if (event.analysis_report) currentReport.value = event.analysis_report
    scrollToBottom()
    return
  }
  if (event.type === 'done') {
    if (!currentReport.value && currentAssistantMessage.value) currentReport.value = currentAssistantMessage.value
    loading.value = false
    currentStreamController.value = null
    finalizeCurrentConversation()
    await Promise.all([fetchConversations(), fetchCollects()])
    return
  }
  if (event.type === 'error') {
    addProgressItem(event)
    if (!currentReport.value) currentReport.value = event.message || '分析过程中发生错误。'
    loading.value = false
    currentStreamController.value = null
    finalizeCurrentConversation()
    await Promise.all([fetchConversations(), fetchCollects()])
    return
  }
  if (['status', 'assistant', 'tool_log', 'message'].includes(event.type)) {
    if (event.type === 'assistant' && event.message) currentAssistantMessage.value = event.message
    addProgressItem(event)
  }
}

const handleStreamError = async (error) => {
  console.error('Agent stream error:', error)
  ElMessage.error(`请求失败: ${error.message || '未知错误'}`)
  addProgressItem({
    type: 'error',
    level: 'error',
    message: '请求失败，无法获取实时分析结果。'
  })
  if (!currentReport.value) currentReport.value = '请求失败了。'
  loading.value = false
  currentStreamController.value = null
  finalizeCurrentConversation()
  await Promise.all([fetchConversations(), fetchCollects()])
}

const handleStreamDone = async () => {
  if (!loading.value) return
  if (!currentReport.value && currentAssistantMessage.value) currentReport.value = currentAssistantMessage.value
  loading.value = false
  currentStreamController.value = null
  finalizeCurrentConversation()
  await Promise.all([fetchConversations(), fetchCollects()])
}

const onSendMessage = (content) => {
  if (!content.trim()) return
  stopCurrentStream()
  resetCurrentConversationState()
  currentQuestion.value = content
  loading.value = true
  addProgressItem({
    type: 'status',
    level: 'info',
    message: '正在建立分析会话并加载上下文...'
  })
  scrollToBottom()

  currentStreamController.value = streamAgent(
    {
      namespace_id: activeNamespace.value,
      conversation_id: activeConversationId.value || '',
      user_message: content,
      datasource: currentDataSource.value
    },
    handleStreamEvent,
    handleStreamError,
    handleStreamDone
  )
}

onMounted(async () => {
  await Promise.all([fetchConversations(), fetchCollects()])
})
</script>

<style scoped>
.app-container { display: flex; height: 100vh; overflow: hidden; }
.main-content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.content-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 12px 16px; background: #fff; border-bottom: 1px solid #e4e7ed; }
.tabs { display: flex; gap: 20px; }
.tab { padding: 8px 12px; font-size: 14px; color: #606266; cursor: pointer; border-bottom: 2px solid transparent; }
.tab.active, .tab:hover { color: #409eff; border-bottom-color: #409eff; }
.header-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; color: #909399; }
.content-body { flex: 1; display: flex; overflow: hidden; }
.left-panel { width: 280px; flex-shrink: 0; overflow: hidden; background: #f5f7fa; }
.right-panel { flex: 1; padding: 16px; background: #f5f7fa; overflow: hidden; }
.chat-window { display: flex; flex-direction: column; height: 100%; background: #fff; border-radius: 8px; overflow: hidden; }
.messages { flex: 1; padding: 16px; overflow-y: auto; }
.input-area { padding: 12px 16px; border-top: 1px solid #e4e7ed; }
.empty-tip { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #909399; }
.tip-icon { width: 56px; height: 56px; border-radius: 18px; display: inline-flex; align-items: center; justify-content: center; margin-bottom: 16px; background: #eef5ff; color: #409eff; font-weight: 700; }
.message { display: flex; gap: 10px; margin-bottom: 16px; }
.message.user { flex-direction: row-reverse; }
.message-avatar { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; flex-shrink: 0; }
.message.user .message-avatar { background: #409eff; color: #fff; }
.message.report .message-avatar, .message.progress-feed .message-avatar { background: #f0f2f5; color: #4f5b6b; }
.message-content { max-width: 80%; padding: 10px 14px; border-radius: 8px; line-height: 1.6; font-size: 14px; word-break: break-word; background: #f5f7fa; color: #303133; }
.message.user .message-content { background: #409eff; color: #fff; }
.message-actions { margin: -8px 0 12px 42px; display: flex; gap: 8px; }
.progress-title { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-weight: 600; }
.progress-subtitle, .progress-empty { font-size: 12px; color: #909399; }
.progress-list, .detail-list, .turn-detail, .detail-section { display: flex; flex-direction: column; gap: 12px; }
.progress-item { padding: 8px 10px; border-radius: 8px; font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
.progress-item.info { background: #eef5ff; color: #2f5f9e; }
.progress-item.success { background: #edf9f0; color: #2f7d4d; }
.progress-item.warning { background: #fff7e8; color: #9a6700; }
.progress-item.error { background: #fff0f0; color: #c45656; }
.progress-item.assistant { background: #f7f4ff; color: #5b45b0; }
.message-chart { margin: 12px 0; min-height: 200px; border-radius: 8px; overflow: hidden; }
.detail-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.detail-title { font-size: 18px; font-weight: 700; color: #1f2a37; }
.detail-meta { font-size: 12px; color: #6b7280; margin-top: 6px; }
.section-title { font-size: 14px; font-weight: 700; color: #374151; }
.detail-card { padding: 14px 16px; border-radius: 12px; background: #f8fafc; color: #1f2937; line-height: 1.7; white-space: pre-wrap; }
.detail-list-item { display: flex; flex-direction: column; gap: 8px; }
.detail-list-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.detail-pill { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-size: 11px; background: #e2e8f0; color: #334155; }
.detail-pill.muted { background: #f1f5f9; color: #64748b; }
.artifact-title { font-weight: 700; margin-bottom: 6px; }
.artifact-summary { margin-bottom: 8px; }
.artifact-link, .detail-empty { font-size: 12px; color: #64748b; }
</style>
