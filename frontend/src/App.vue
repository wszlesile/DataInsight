<template>
  <div class="app-container">
    <Sidebar @select-space="onSelectSpace" @navigate="onNavigate" />

    <div class="main-content">
      <div class="content-header">
        <div class="tabs">
          <span
            v-for="tab in tabs"
            :key="tab.id"
            class="tab"
            :class="{ active: activeTab === tab.id }"
            @click="activeTab = tab.id"
          >
            {{ tab.name }}
          </span>
        </div>
      </div>

      <div class="content-body">
        <div class="left-panel">
          <DataSourcePanel @data-source-change="onDataSourceChange" />
        </div>

        <div class="right-panel">
          <div class="chat-window">
            <div class="messages" ref="messagesContainer">
              <template v-for="(item, index) in chatHistory" :key="index">
                <div class="message user">
                  <div class="message-avatar">👤</div>
                  <div class="message-content">{{ item.question }}</div>
                </div>

                <div class="message progress-feed" v-if="item.progressItems?.length">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content progress-content">
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

                <div class="message-chart" v-if="item.chartUrl">
                  <ChartDisplay :chart-url="item.chartUrl" />
                </div>

                <div class="message report" v-if="item.report">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content" v-html="renderMarkdown(item.report)" />
                </div>
              </template>

              <template v-if="currentQuestion">
                <div class="message user">
                  <div class="message-avatar">👤</div>
                  <div class="message-content">{{ currentQuestion }}</div>
                </div>

                <div class="message progress-feed" v-if="loading || currentProgressItems.length">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content progress-content">
                    <div class="progress-title">
                      系统工作中
                      <span class="progress-subtitle" v-if="loading">实时更新中...</span>
                    </div>
                    <div class="progress-list" v-if="currentProgressItems.length">
                      <div
                        v-for="progress in currentProgressItems"
                        :key="progress.id"
                        class="progress-item"
                        :class="progress.level"
                      >
                        {{ progress.message }}
                      </div>
                    </div>
                    <div class="progress-empty" v-else>正在准备分析上下文...</div>
                  </div>
                </div>

                <div class="message-chart" v-if="currentChartUrl">
                  <ChartDisplay :chart-url="currentChartUrl" />
                </div>

                <div class="message report" v-if="currentReport">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content" v-html="renderMarkdown(currentReport)" />
                </div>
              </template>

              <div class="empty-tip" v-if="chatHistory.length === 0 && !currentQuestion">
                <span class="tip-icon">💬</span>
                <p>输入你的问题，开始数据分析</p>
              </div>
            </div>

            <div class="input-area">
              <ChatInput :loading="loading" @send="onSendMessage" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import { marked } from 'marked'
import { ElMessage } from 'element-plus'

import Sidebar from './components/Sidebar.vue'
import DataSourcePanel from './components/DataSourcePanel.vue'
import ChatInput from './components/ChatInput.vue'
import ChartDisplay from './components/ChartDisplay.vue'
import { streamAgent } from './api/agent.js'

marked.setOptions({
  breaks: true,
  gfm: true
})

const renderMarkdown = (content) => {
  if (!content) return ''
  return marked.parse(content)
}

const tabs = ref([
  { id: '1', name: '销售数据分析' },
  { id: '2', name: '报警数据分析' }
])

const activeTab = ref('1')
const loading = ref(false)
const chatHistory = ref([])
const currentQuestion = ref('')
const currentChartUrl = ref('')
const currentReport = ref('')
const currentAssistantMessage = ref('')
const currentDataSource = ref(null)
const currentProgressItems = ref([])
const currentStreamController = ref(null)
const messagesContainer = ref(null)

const createProgressItem = (event) => ({
  id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
  level: event.level || (event.type === 'assistant' ? 'assistant' : 'info'),
  message: event.message
})

const addProgressItem = (event) => {
  if (!event?.message) return

  const lastItem = currentProgressItems.value[currentProgressItems.value.length - 1]
  if (lastItem && lastItem.message === event.message) return

  currentProgressItems.value.push(createProgressItem(event))
  scrollToBottom()
}

const normalizeFileUrl = (fileId) => `/files/${encodeURIComponent(fileId)}`

const pushCurrentConversationToHistory = () => {
  if (!currentQuestion.value) return

  chatHistory.value.push({
    question: currentQuestion.value,
    chartUrl: currentChartUrl.value,
    report: currentReport.value,
    progressItems: [...currentProgressItems.value]
  })
}

const resetCurrentConversation = () => {
  currentQuestion.value = ''
  currentChartUrl.value = ''
  currentReport.value = ''
  currentAssistantMessage.value = ''
  currentProgressItems.value = []
}

const stopCurrentStream = () => {
  if (currentStreamController.value) {
    currentStreamController.value.abort()
    currentStreamController.value = null
  }
}

const onSelectSpace = (space) => {
  stopCurrentStream()
  activeTab.value = space.id
  chatHistory.value = []
  resetCurrentConversation()
  loading.value = false
}

const onNavigate = (page) => {
  console.log('Navigate to:', page)
}

const onDataSourceChange = (dataSource) => {
  currentDataSource.value = dataSource
}

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

const handleStreamEvent = (event) => {
  if (!event || typeof event !== 'object') return

  if (event.type === 'result') {
    if (event.file_id) {
      currentChartUrl.value = normalizeFileUrl(event.file_id)
    }
    if (event.analysis_report) {
      currentReport.value = event.analysis_report
    }
    scrollToBottom()
    return
  }

  if (event.type === 'done') {
    if (!currentReport.value && currentAssistantMessage.value) {
      currentReport.value = currentAssistantMessage.value
    }
    loading.value = false
    currentStreamController.value = null
    scrollToBottom()
    return
  }

  if (event.type === 'error') {
    addProgressItem(event)
    if (!currentReport.value) {
      currentReport.value = event.message || '分析过程中发生错误。'
    }
    loading.value = false
    currentStreamController.value = null
    return
  }

  if (['status', 'assistant', 'tool_log', 'message'].includes(event.type)) {
    if (event.type === 'assistant' && event.message) {
      currentAssistantMessage.value = event.message
    }
    addProgressItem(event)
  }
}

const handleStreamError = (error) => {
  console.error('Agent stream error:', error)
  ElMessage.error('请求失败: ' + (error.message || '未知错误'))
  addProgressItem({
    type: 'error',
    level: 'error',
    message: '请求失败，无法获取实时分析结果。'
  })
  if (!currentReport.value) {
    currentReport.value = '请求失败了。'
  }
  loading.value = false
  currentStreamController.value = null
}

const handleStreamDone = () => {
  if (!currentReport.value && currentAssistantMessage.value) {
    currentReport.value = currentAssistantMessage.value
  }
  loading.value = false
  currentStreamController.value = null
  scrollToBottom()
}

const onSendMessage = async (content) => {
  if (!content.trim()) return

  stopCurrentStream()

  if (currentQuestion.value) {
    pushCurrentConversationToHistory()
  }

  resetCurrentConversation()
  currentQuestion.value = content
  loading.value = true
  addProgressItem({
    type: 'status',
    level: 'info',
    message: '正在建立流式分析连接...'
  })
  scrollToBottom()

  currentStreamController.value = streamAgent(
    {
      username: 'user',
      namespace_id: activeTab.value,
      conversation_id: '',
      user_message: content
    },
    handleStreamEvent,
    handleStreamError,
    handleStreamDone
  )
}
</script>

<style scoped>
.app-container {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.content-header {
  padding: 12px 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}

.tabs {
  display: flex;
  gap: 20px;
}

.tab {
  padding: 8px 12px;
  font-size: 14px;
  color: #606266;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}

.tab:hover {
  color: #409eff;
}

.tab.active {
  color: #409eff;
  border-bottom-color: #409eff;
}

.content-body {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.left-panel {
  width: 280px;
  flex-shrink: 0;
  overflow: hidden;
  background: #f5f7fa;
}

.right-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 16px;
  background: #f5f7fa;
}

.chat-window {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
}

.messages {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
}

.empty-tip {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #909399;
}

.tip-icon {
  font-size: 48px;
  margin-bottom: 16px;
}

.message {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}

.message.user {
  flex-direction: row-reverse;
}

.message-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}

.message.user .message-avatar {
  background: #409eff;
}

.message.report .message-avatar,
.message.progress-feed .message-avatar {
  background: #f0f2f5;
}

.message-content {
  max-width: 80%;
  padding: 10px 14px;
  border-radius: 8px;
  line-height: 1.6;
  font-size: 14px;
  word-break: break-word;
}

.message.user .message-content {
  background: #409eff;
  color: #fff;
}

.message.report .message-content,
.message.progress-feed .message-content {
  background: #f5f7fa;
  color: #303133;
}

.progress-content {
  min-width: 360px;
}

.progress-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 10px;
}

.progress-subtitle {
  font-size: 12px;
  color: #909399;
  font-weight: 400;
}

.progress-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.progress-item {
  padding: 8px 10px;
  border-radius: 8px;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.5;
}

.progress-item.info {
  background: #eef5ff;
  color: #2f5f9e;
}

.progress-item.success {
  background: #edf9f0;
  color: #2f7d4d;
}

.progress-item.warning {
  background: #fff7e8;
  color: #9a6700;
}

.progress-item.error {
  background: #fff0f0;
  color: #c45656;
}

.progress-item.assistant {
  background: #f7f4ff;
  color: #5b45b0;
}

.progress-empty {
  color: #909399;
  font-size: 13px;
}

.message-chart {
  margin: 12px 0;
  min-height: 200px;
  border-radius: 8px;
  overflow: hidden;
}

.input-area {
  flex-shrink: 0;
  padding: 12px 16px;
  border-top: 1px solid #e4e7ed;
}

.message-content :deep(h1),
.message-content :deep(h2),
.message-content :deep(h3),
.message-content :deep(h4) {
  margin-top: 8px;
  margin-bottom: 8px;
  font-weight: 600;
}

.message-content :deep(h1) { font-size: 18px; }
.message-content :deep(h2) { font-size: 16px; }
.message-content :deep(h3) { font-size: 14px; }

.message-content :deep(p) {
  margin: 8px 0;
}

.message-content :deep(ul),
.message-content :deep(ol) {
  margin: 8px 0;
  padding-left: 20px;
}

.message-content :deep(li) {
  margin: 4px 0;
}

.message-content :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
}

.message-content :deep(th),
.message-content :deep(td) {
  border: 1px solid #e4e7ed;
  padding: 6px 10px;
  text-align: left;
}

.message-content :deep(th) {
  background: #f5f7fa;
}

.message-content :deep(code) {
  background: #f0f2f5;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: monospace;
}

.message-content :deep(blockquote) {
  border-left: 3px solid #409eff;
  margin: 8px 0;
  padding-left: 12px;
  color: #606266;
}

.message.user .message-content :deep(h1),
.message.user .message-content :deep(h2),
.message.user .message-content :deep(h3),
.message.user .message-content :deep(p),
.message.user .message-content :deep(ul),
.message.user .message-content :deep(ol),
.message.user .message-content :deep(li),
.message.user .message-content :deep(table),
.message.user .message-content :deep(th),
.message.user .message-content :deep(td),
.message.user .message-content :deep(code),
.message.user .message-content :deep(blockquote) {
  color: #fff;
}

.message.user .message-content :deep(th) {
  background: rgba(255, 255, 255, 0.2);
}

.message.user .message-content :deep(code) {
  background: rgba(255, 255, 255, 0.2);
}
</style>
