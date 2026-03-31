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
        <!-- 左侧：数据源 -->
        <div class="left-panel">
          <DataSourcePanel @data-source-change="onDataSourceChange" />
        </div>

        <!-- 右侧：聊天窗口 -->
        <div class="right-panel">
          <div class="chat-window">
            <!-- 消息列表 -->
            <div class="messages" ref="messagesContainer">
              <!-- 遍历所有对话记录 -->
              <template v-for="(item, index) in chatHistory" :key="index">
                <!-- 用户消息 -->
                <div class="message user">
                  <div class="message-avatar">👤</div>
                  <div class="message-content">{{ item.question }}</div>
                </div>

                <!-- 图表 -->
                <div class="message-chart" v-if="item.chartUrl">
                  <ChartDisplay :chart-url="item.chartUrl" />
                </div>

                <!-- 分析报告 -->
                <div class="message report" v-if="item.report">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content" v-html="renderMarkdown(item.report)" />
                </div>
              </template>

              <!-- 当前进行中的对话 -->
              <template v-if="currentQuestion">
                <!-- 用户消息 -->
                <div class="message user">
                  <div class="message-avatar">👤</div>
                  <div class="message-content">{{ currentQuestion }}</div>
                </div>

                <!-- 加载状态 -->
                <div class="message assistant loading" v-if="loading">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content">分析中...</div>
                </div>

                <!-- 当前图表 -->
                <div class="message-chart" v-if="currentChartUrl && !loading">
                  <ChartDisplay :chart-url="currentChartUrl" />
                </div>

                <!-- 当前分析报告 -->
                <div class="message report" v-if="currentReport && !loading">
                  <div class="message-avatar">🤖</div>
                  <div class="message-content" v-html="renderMarkdown(currentReport)" />
                </div>
              </template>

              <!-- 空状态提示 -->
              <div class="empty-tip" v-if="chatHistory.length === 0 && !currentQuestion">
                <span class="tip-icon">💬</span>
                <p>输入您的问题，开始数据分析</p>
              </div>
            </div>

            <!-- 输入框 -->
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
import Sidebar from './components/Sidebar.vue'
import DataSourcePanel from './components/DataSourcePanel.vue'
import ChatInput from './components/ChatInput.vue'
import ChartDisplay from './components/ChartDisplay.vue'
import { invokeAgent } from './api/agent.js'
import { ElMessage } from 'element-plus'

// 配置 marked
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
const chatHistory = ref([])  // 历史对话记录 [{question, chartUrl, report}]
const currentQuestion = ref('')
const currentChartUrl = ref('')
const currentReport = ref('')
const currentDataSource = ref(null)
const messagesContainer = ref(null)

const onSelectSpace = (space) => {
  activeTab.value = space.id
  chatHistory.value = []
  currentQuestion.value = ''
  currentChartUrl.value = ''
  currentReport.value = ''
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

const onSendMessage = async (content) => {
  if (!content.trim()) return

  // 保存当前对话到历史（只有已完成的内容才保存）
  // 如果已经有进行中的对话，先保存
  if (currentQuestion.value) {
    chatHistory.value.push({
      question: currentQuestion.value,
      chartUrl: currentChartUrl.value,
      report: currentReport.value
    })
  }

  // 设置当前对话
  currentQuestion.value = content
  currentChartUrl.value = ''
  currentReport.value = ''

  scrollToBottom()
  loading.value = true

  try {
    const response = await invokeAgent({
      username: 'user',
      namespace_id: activeTab.value,
      conversation_id: '',
      user_message: content
    })

    if (response.data.success) {
      const data = response.data.data

      if (data.file_id) {
        currentChartUrl.value = `/files/${encodeURIComponent(data.file_id)}`
      }

      if (data.analysis_report) {
        currentReport.value = data.analysis_report
      } else if (data.message) {
        currentReport.value = data.message
      } else {
        currentReport.value = '已完成分析'
      }
    } else {
      ElMessage.error(response.data.message || '分析失败')
      currentReport.value = '分析失败，请稍后重试。'
    }
  } catch (error) {
    console.error('Agent invoke error:', error)
    ElMessage.error('请求失败: ' + (error.message || '未知错误'))
    currentReport.value = '请求失败了。'
  } finally {
    loading.value = false
    scrollToBottom()
  }
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

.message.report .message-avatar {
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

.message.report .message-content {
  background: #f5f7fa;
  color: #303133;
}

.message.loading .message-content {
  color: #909399;
  font-style: italic;
  background: #f0f2f5;
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

/* Markdown 样式 */
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