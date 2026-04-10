<template>
  <div class="app-shell">
    <header class="top-nav">
      <div class="logo">
        <div class="logo-icon">DI</div>
        <div>
          <div class="logo-title">Data Insight</div>
          <div class="logo-subtitle">数据洞察智能助手</div>
        </div>
      </div>

      <div class="nav-actions">
        <button class="nav-icon" type="button" @click="showHelp">?</button>
        <button class="nav-icon" type="button" @click="favoritesPanelVisible = true">
          ★
          <span v-if="collects.length" class="badge">{{ collects.length }}</span>
        </button>
        <div class="user-avatar">AI</div>
      </div>
    </header>

    <div class="main-container">
      <Sidebar
        :spaces="spaces"
        :active-space="activeNamespace"
        :conversations="conversations"
        :collects="collects"
        :active-conversation-id="activeConversationId"
        @select-space="onSelectSpace"
        @select-conversation="onSelectConversation"
        @delete-conversation="onDeleteConversation"
        @delete-space="onDeleteSpace"
        @rename-space="onRenameSpace"
        @new-space="onNewSpace"
        @new-conversation="onNewConversation"
        @open-favorites="favoritesPanelVisible = true"
        @open-knowledge="showKnowledgePlaceholder"
      />

      <div class="right-area">
        <DataSourcePanel
          :active-namespace-id="activeNamespace"
          :active-space-name="activeSpaceName"
          :active-conversation="activeConversation"
          :selected-data-source="currentDataSource"
          @data-source-change="onDataSourceChange"
        />

        <section class="chat-panel">
          <div class="chat-header">
            <div class="chat-title">
              <span class="chat-title-icon">🤖</span>
              <div>
                <div>数据洞察助手</div>
                <div class="chat-subtitle">{{ activeConversation?.title || '新会话已创建，输入问题开始分析' }}</div>
              </div>
            </div>

            <div class="chat-header-actions">
              <button
                v-if="activeConversation"
                class="action-btn"
                type="button"
                @click="onRenameConversation(activeConversation)"
              >
                重命名
              </button>
              <button
                v-if="activeConversation"
                class="action-btn"
                type="button"
                @click="toggleConversationCollect"
              >
                {{ isCollected('conversation', activeConversation.id) ? '取消收藏会话' : '收藏会话' }}
              </button>
            </div>
          </div>

          <div ref="messagesContainer" class="chat-messages">
            <div v-if="chatHistory.length === 0 && !currentQuestion" class="welcome-card">
              <div class="welcome-icon">📊</div>
              <div class="welcome-title">你好，我是数据洞察助手</div>
              <div class="welcome-subtitle">
                我会结合当前洞察空间、会话上下文和分析执行能力，帮助你完成多轮持续的数据分析与图表生成。
              </div>
              <div class="quick-actions">
                <button
                  v-for="prompt in quickPrompts"
                  :key="prompt"
                  class="quick-action"
                  type="button"
                  @click="useQuickPrompt(prompt)"
                >
                  {{ prompt }}
                </button>
              </div>
            </div>

            <template v-for="(item, index) in chatHistory" :key="`${item.turnId || index}-${index}`">
              <div class="chat-message user">
                <div class="message-avatar user">U</div>
                <div class="message-bubble user">{{ item.question }}</div>
              </div>

              <div v-if="item.progressItems?.length" class="chat-message ai">
                <div class="message-avatar ai">AI</div>
                <div class="message-body">
                  <div class="progress-card">
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
              </div>

              <div v-if="getDisplayCharts(item).length || item.report" class="chat-message ai">
                <div class="message-avatar ai">AI</div>
                <div class="message-body">
                  <div class="result-card answer-card">
                    <div class="result-header">
                      <div class="result-title">分析结果</div>
                      <div class="result-actions">
                        <button class="action-btn" type="button" @click="openTurnDetail(item.turnId)">查看详情</button>
                        <button
                          v-if="getPrimaryChart(item)"
                          class="action-btn"
                          type="button"
                          @click="onDownloadChart({ chart: getPrimaryChart(item), title: getPrimaryChart(item)?.title || 'analysis-chart' })"
                        >
                          下载图表
                        </button>
                        <button
                          class="action-btn"
                          type="button"
                          @click="onExportAnalysisPdf({ turnId: item.turnId })"
                        >
                          导出 PDF
                        </button>
                        <button class="action-btn" type="button" @click="onRerunTurn(item)">刷新分析</button>
                        <button
                          class="action-btn"
                          type="button"
                          @click="toggleCollect({
                            collectType: 'turn',
                            targetId: item.turnId,
                            title: item.question,
                            summaryText: item.report,
                            conversationId: activeConversationId,
                            metadata: {
                              turn_id: item.turnId,
                              charts: item.charts || [],
                              tables: item.tables || []
                            }
                          })"
                        >
                          {{ isCollected('turn', item.turnId) ? '取消收藏' : '收藏本轮' }}
                        </button>
                        <button
                          v-if="getPrimaryChart(item)"
                          class="action-btn"
                          type="button"
                          @click="toggleChartCollectForTurn(item)"
                        >
                          {{ item.chartArtifactId && isCollected('artifact', item.chartArtifactId) ? '取消收藏图表' : '收藏图表' }}
                        </button>
                      </div>
                    </div>

                    <div v-if="getDisplayCharts(item).length" class="answer-chart-grid">
                      <div
                        v-for="(chart, chartIndex) in getDisplayCharts(item)"
                        :key="`${item.turnId}-chart-${chart.id || chartIndex}`"
                        class="answer-chart"
                      >
                        <ChartDisplay :chart-spec="chart.chartSpec" />
                      </div>
                    </div>

                    <div
                      v-if="item.report"
                      class="message-bubble ai report-text answer-report"
                      v-html="renderMarkdown(item.report)"
                    />
                  </div>
                </div>
              </div>
            </template>

            <template v-if="currentQuestion">
              <div class="chat-message user">
                <div class="message-avatar user">U</div>
                <div class="message-bubble user">{{ currentQuestion }}</div>
              </div>

              <div v-if="loading || currentProgressItems.length" class="chat-message ai">
                <div class="message-avatar ai">AI</div>
                <div class="message-body">
                  <div class="progress-card">
                    <div class="progress-title">系统工作流</div>
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
              </div>

              <div v-if="currentCharts.length || currentReport" class="chat-message ai">
                <div class="message-avatar ai">AI</div>
                <div class="message-body">
                  <div class="result-card answer-card">
                    <div class="result-header">
                      <div class="result-title">本轮分析结果</div>
                      <div class="result-actions">
                        <button v-if="currentTurnId" class="action-btn" type="button" @click="openTurnDetail(currentTurnId)">
                          查看详情
                        </button>
                        <button
                          v-if="getPrimaryChart({ charts: currentCharts })"
                          class="action-btn"
                          type="button"
                          @click="onDownloadChart({ chart: getPrimaryChart({ charts: currentCharts }), title: getPrimaryChart({ charts: currentCharts })?.title || 'analysis-chart' })"
                        >
                          下载图表
                        </button>
                        <button
                          class="action-btn"
                          type="button"
                          @click="onExportAnalysisPdf({ turnId: currentTurnId })"
                        >
                          导出 PDF
                        </button>
                        <button
                          v-if="currentTurnId"
                          class="action-btn"
                          type="button"
                          @click="onRerunTurn({ turnId: currentTurnId, question: currentQuestion, charts: [...currentCharts], chartArtifactId: currentChartArtifactId, report: currentReport })"
                        >
                          刷新分析
                        </button>
                        <button
                          v-if="currentTurnId"
                          class="action-btn"
                          type="button"
                          @click="toggleCollect({
                            collectType: 'turn',
                            targetId: currentTurnId,
                            title: currentQuestion,
                            summaryText: currentReport,
                            conversationId: activeConversationId,
                            metadata: {
                              turn_id: currentTurnId,
                              charts: [...currentCharts],
                              tables: [...currentTables]
                            }
                          })"
                        >
                          {{ isCollected('turn', currentTurnId) ? '取消收藏' : '收藏本轮' }}
                        </button>
                        <button
                          v-if="currentTurnId && getPrimaryChart({ charts: currentCharts })"
                          class="action-btn"
                          type="button"
                          @click="toggleCurrentChartCollect"
                        >
                          {{ currentChartArtifactId && isCollected('artifact', currentChartArtifactId) ? '取消收藏图表' : '收藏图表' }}
                        </button>
                      </div>
                    </div>

                    <div v-if="currentCharts.length" class="answer-chart-grid">
                      <div
                        v-for="(chart, chartIndex) in currentCharts"
                        :key="`${currentTurnId}-chart-${chart.id || chartIndex}`"
                        class="answer-chart"
                      >
                        <ChartDisplay :chart-spec="chart.chartSpec" />
                      </div>
                    </div>
                    <div
                      v-if="currentReport"
                      class="message-bubble ai report-text answer-report"
                      v-html="renderMarkdown(currentReport)"
                    />
                  </div>
                </div>
              </div>
            </template>
          </div>

          <ChatInput :loading="loading" @send="onSendMessage" />
        </section>
      </div>
    </div>

    <div v-if="favoritesPanelVisible" class="favorites-mask" @click="favoritesPanelVisible = false" />
    <aside class="favorites-panel" :class="{ show: favoritesPanelVisible }">
      <div class="favorites-header">
        <h3>★ 我的收藏</h3>
        <button class="favorites-close" type="button" @click="favoritesPanelVisible = false">×</button>
      </div>

      <div class="favorites-content">
        <template v-if="visibleFavorites.length">
          <div
            v-for="collect in visibleFavorites"
            :key="`enhanced-${collect.id}`"
            class="favorite-item"
            :class="`favorite-item-${collect.collect_type}`"
            @click="onSelectCollect(collect)"
          >
            <template v-if="collect.collect_type === 'turn'">
              <div class="favorite-header-row">
                <div class="favorite-title">
                  <span class="favorite-type-icon">📑</span>
                  {{ collect.title || `收藏 ${collect.id}` }}
                </div>
                <span class="detail-pill">{{ favoriteTypeLabel(collect) }}</span>
              </div>

              <div v-if="favoriteChartSpec(collect)" class="favorite-turn-chart">
                <ChartDisplay :chart-spec="favoriteChartSpec(collect)" />
              </div>

              <div
                v-if="collect.summary_text"
                class="message-bubble ai report-text favorite-result-report"
                v-html="renderMarkdown(collect.summary_text)"
              />

              <div class="favorite-meta">
                <span>Turn ID {{ collect.target_id }}</span>
                <span>{{ formatDateTime(collect.created_at) }}</span>
              </div>
            </template>

            <template v-else-if="collect.collect_type === 'artifact'">
              <div class="favorite-header-row">
                <div class="favorite-title">
                  <span class="favorite-type-icon">📊</span>
                  {{ collect.title || `收藏 ${collect.id}` }}
                </div>
                <span class="detail-pill">{{ favoriteTypeLabel(collect) }}</span>
              </div>

              <div class="favorite-preview favorite-artifact-preview">
                <ChartDisplay
                  v-if="favoriteChartSpec(collect)"
                  :chart-spec="favoriteChartSpec(collect)"
                />
                <div v-else class="chart-placeholder">
                  <span class="bar" style="height: 26px;" />
                  <span class="bar" style="height: 44px;" />
                  <span class="bar" style="height: 34px;" />
                  <span class="bar" style="height: 58px;" />
                </div>
              </div>

              <div class="favorite-meta">
                <span>Artifact ID {{ collect.target_id }}</span>
                <span>{{ formatDateTime(collect.created_at) }}</span>
              </div>
            </template>

            <template v-else>
              <div class="favorite-header-row">
                <div class="favorite-title">
                  <span class="favorite-type-icon">💬</span>
                  {{ collect.title || `收藏 ${collect.id}` }}
                </div>
                <span class="detail-pill">{{ favoriteTypeLabel(collect) }}</span>
              </div>

              <div class="favorite-preview">
                <div class="favorite-preview-text">会话收藏可直接打开并继续对话。</div>
              </div>

              <div class="favorite-meta">
                <span>Conversation ID {{ collect.target_id }}</span>
                <span>{{ formatDateTime(collect.created_at) }}</span>
              </div>
            </template>
          </div>

        </template>
        <div v-else class="favorites-empty">
          <div class="empty-icon">📁</div>
          <p>当前还没有可展示的收藏</p>
          <span>整体分析结果会展示在“分析结果”，单独图表会展示在“图表收藏”。</span>
        </div>
      </div>
    </aside>

    <el-drawer v-model="turnDetailVisible" title="轮次详情" size="48%" destroy-on-close>
      <div v-if="turnDetailLoading" class="detail-empty">正在加载详情...</div>
      <div v-else-if="turnDetail" class="turn-detail">
        <div class="detail-header">
          <div>
            <div class="detail-title">第 {{ turnDetail.turn.turn_no }} 轮分析</div>
            <div class="detail-meta">状态：{{ turnDetail.turn.status }} | 开始时间：{{ formatDateTime(turnDetail.turn.started_at) }}</div>
          </div>
          <button
            class="action-btn"
            type="button"
            @click="toggleCollect({
              collectType: 'turn',
              targetId: turnDetail.turn.id,
              title: turnDetail.turn.user_query,
              summaryText: turnDetail.turn.final_answer,
              conversationId: turnDetail.conversation.id,
              metadata: {
                turn_id: turnDetail.turn.id,
                charts: (turnDetail.artifacts || [])
                  .filter((artifact) => artifact.artifact_type === 'chart')
                  .map((artifact) => ({
                    chart_spec: artifactChartSpec(artifact),
                    title: artifact.title || ''
                  }))
              }
            })"
          >
            {{ isCollected('turn', turnDetail.turn.id) ? '取消收藏' : '收藏本轮' }}
          </button>
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
                <button
                  v-if="artifact.artifact_type === 'chart' && artifactChartSpec(artifact)"
                  class="action-btn"
                  type="button"
                  @click="previewChartArtifact(artifact)"
                >
                  预览
                </button>
                <button
                  class="action-btn"
                  type="button"
                  @click="toggleCollect({
                    collectType: 'artifact',
                    targetId: artifact.id,
                    title: artifact.title,
                    summaryText: artifact.summary_text,
                    conversationId: turnDetail.conversation.id,
                    artifactId: artifact.id,
                    metadata: {
                      turn_id: turnDetail.turn.id,
                      chart_spec: artifactChartSpec(artifact)
                    }
                  })"
                >
                  {{ isCollected('artifact', artifact.id) ? '取消收藏' : '收藏产物' }}
                </button>
              </div>
              <div class="detail-card">
                <div class="artifact-title">{{ artifact.title || '未命名产物' }}</div>
                <div v-if="artifact.summary_text" class="artifact-summary">{{ artifact.summary_text }}</div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="previewArtifact && artifactChartSpec(previewArtifact)" class="detail-section">
          <div class="section-title">产物预览</div>
          <div class="detail-card">
            <div class="artifact-title">{{ previewArtifact.title || '图表预览' }}</div>
            <ChartDisplay :chart-spec="artifactChartSpec(previewArtifact)" />
          </div>
        </div>
      </div>
      <div v-else class="detail-empty">没有可展示的轮次详情。</div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { marked } from 'marked'
import { ElMessage, ElMessageBox } from 'element-plus'

import Sidebar from './components/Sidebar.vue'
import DataSourcePanel from './components/DataSourcePanel.vue'
import ChatInput from './components/ChatInput.vue'
import ChartDisplay from './components/ChartDisplay.vue'
import {
  createConversation,
  createNamespace,
  createCollect,
  deleteConversation,
  deleteNamespace,
  exportTurnPdf,
  getConversationHistory,
  getTurnDetail,
  listCollects,
  listConversations,
  listNamespaces,
  removeCollect,
  renameConversation,
  renameNamespace,
  streamAgent,
  streamRerunTurn
} from './api/agent.js'

marked.setOptions({ breaks: true, gfm: true })

const spaces = ref([])
const activeNamespace = ref('')
const conversations = ref([])
const collects = ref([])
const activeConversationId = ref(0)
const loading = ref(false)
const chatHistory = ref([])
const currentQuestion = ref('')
const currentChartArtifactId = ref(0)
const currentCharts = ref([])
const currentTables = ref([])
const currentReport = ref('')
const currentAssistantMessage = ref('')
const currentDataSource = ref(null)
const currentProgressItems = ref([])
const currentStreamController = ref(null)
const currentTurnId = ref(0)
const currentRunMode = ref('new')
const rerunSourceTurnId = ref(0)
const messagesContainer = ref(null)
const turnDetailVisible = ref(false)
const turnDetailLoading = ref(false)
const turnDetail = ref(null)
const previewArtifact = ref(null)
const favoritesPanelVisible = ref(false)

const quickPrompts = [
  '分析2024年Q4季度的销售趋势',
  '前天一共有多少个报警？看一下明细',
  '总结当前会话里最重要的洞察结论'
]

const activeConversation = computed(() =>
  conversations.value.find((item) => item.id === activeConversationId.value) || null
)

const activeSpaceName = computed(() =>
  spaces.value.find((item) => String(item.id) === activeNamespace.value)?.name || ''
)

const visibleFavorites = computed(() => collects.value)

const renderMarkdown = (content) => (content ? marked.parse(content) : '')
const collectKey = (collectType, targetId) => `${collectType}:${targetId}`
const parseCollectMetadata = (collect) => {
  if (!collect?.metadata_json) return {}
  if (typeof collect.metadata_json === 'string') {
    try {
      return JSON.parse(collect.metadata_json || '{}')
    } catch (error) {
      console.error('Parse collect metadata error:', error)
      return {}
    }
  }
  return collect.metadata_json || {}
}

const favoriteChartSpec = (collect) => {
  const metadata = parseCollectMetadata(collect)
  if (metadata.chart_spec && typeof metadata.chart_spec === 'object') {
    return sanitizeChartSpec(metadata.chart_spec)
  }
  if (Array.isArray(metadata.charts) && metadata.charts.length > 0) {
    return sanitizeChartSpec(metadata.charts[0]?.chartSpec || metadata.charts[0]?.chart_spec || null)
  }
  return null
}

const sanitizeChartSpec = (chartSpec) => {
  if (!chartSpec || typeof chartSpec !== 'object') return null
  const normalized = JSON.parse(JSON.stringify(chartSpec))

  normalized.animation = false
  normalized.animationDuration = 0
  normalized.animationDurationUpdate = 0
  normalized.animationDelay = 0
  normalized.animationDelayUpdate = 0

  const axisKeys = ['xAxis', 'yAxis']
  axisKeys.forEach((axisKey) => {
    const axes = normalized[axisKey]
    const axisList = Array.isArray(axes) ? axes : (axes && typeof axes === 'object' ? [axes] : [])
    axisList.forEach((axis) => {
      if (!axis || typeof axis !== 'object') return
      axis.animation = false
      axis.animationDuration = 0
      axis.animationDurationUpdate = 0
      axis.animationDelay = 0
      axis.animationDelayUpdate = 0
    })
  })

  if (Array.isArray(normalized.series)) {
    normalized.series.forEach((series) => {
      if (!series || typeof series !== 'object') return
      series.animation = false
      if (series.type === 'line' && series.lineStyle && typeof series.lineStyle === 'object' && series.lineStyle.show === false) {
        delete series.lineStyle.show
        if (Object.keys(series.lineStyle).length === 0) {
          delete series.lineStyle
        }
      }
    })
  }

  return normalized
}

const normalizeChartItem = (chart) => {
  if (!chart || typeof chart !== 'object') return null
  return {
    id: chart.id || 0,
    title: chart.title || '',
    chartType: chart.chart_type || '',
    chartSpec: sanitizeChartSpec(chart.chart_spec || null),
    summaryText: chart.summary_text || chart.description || '',
    sortNo: chart.sort_no || 0
  }
}

const normalizeCharts = (charts = []) => {
  const normalized = (Array.isArray(charts) ? charts : [])
    .map((chart) => normalizeChartItem(chart))
    .filter(Boolean)
    .sort((left, right) => (left.sortNo || 0) - (right.sortNo || 0))

  return normalized
}

const getDisplayCharts = (item) => item?.charts || []
const getPrimaryChart = (item) => getDisplayCharts(item)[0] || null
const artifactChartSpec = (artifact) => sanitizeChartSpec(artifact?.content_json?.chart_spec || null)

const favoriteTypeLabel = (collect) => {
  if (collect.collect_type === 'turn') return '整体结果'
  if (collect.collect_type === 'artifact') return '图表收藏'
  if (collect.collect_type === 'conversation') return '会话收藏'
  return collect.collect_type || '收藏'
}

const buildChartDownloadFilename = (title = 'analysis-chart') =>
  `${String(title || 'analysis-chart')
    .replace(/[\\\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, '-')
    .slice(0, 80) || 'analysis-chart'}.png`

const getChartImageDataUrl = (chart) => new Promise((resolve, reject) => {
  if (!chart) {
    reject(new Error('当前没有可下载的图表'))
    return
  }

  if (chart.chartSpec && typeof chart.chartSpec === 'object' && Object.keys(chart.chartSpec).length > 0) {
    const container = document.createElement('div')
    container.style.position = 'fixed'
    container.style.left = '-99999px'
    container.style.top = '0'
    container.style.width = '960px'
    container.style.height = '540px'
    container.style.background = '#ffffff'
    document.body.appendChild(container)

    try {
      const instance = echarts.init(container, null, { renderer: 'canvas' })
      let settled = false
      const cleanup = () => {
        if (settled) return
        settled = true
        instance.dispose()
        if (container.parentNode) {
          container.parentNode.removeChild(container)
        }
      }
      instance.on('finished', () => {
        try {
          const dataUrl = instance.getDataURL({
            type: 'png',
            pixelRatio: 2,
            backgroundColor: '#ffffff'
          })
          cleanup()
          resolve(dataUrl)
        } catch (error) {
          cleanup()
          reject(error)
        }
      })
      instance.setOption(chart.chartSpec, true)
      window.setTimeout(() => {
        if (settled) return
        try {
          const dataUrl = instance.getDataURL({
            type: 'png',
            pixelRatio: 2,
            backgroundColor: '#ffffff'
          })
          cleanup()
          resolve(dataUrl)
        } catch (error) {
          cleanup()
          reject(error)
        }
      }, 600)
      return
    } catch (error) {
      document.body.removeChild(container)
      reject(error)
      return
    }
  }
  reject(new Error('当前图表缺少可下载的结构化配置'))
})

const downloadChartAsImage = async (chart, filename) => {
  const dataUrl = await getChartImageDataUrl(chart)
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
}

const createProgressItem = (event) => ({
  id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
  level: event.level || (event.type === 'assistant' ? 'assistant' : 'info'),
  message: event.message
})

const isInternalAssistantMessage = (message) => {
  if (!message) return false
  const text = String(message)
  return (
    text.includes('<tool_call>') ||
    text.includes('save_analysis_result(') ||
    text.includes('load_data_with_') ||
    text.includes('import pandas as pd') ||
    text.includes('"tool": "execute_python"') ||
    text.includes('"status": "failed"')
  )
}

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
  currentChartArtifactId.value = 0
  currentCharts.value = []
  currentTables.value = []
  currentReport.value = ''
  currentAssistantMessage.value = ''
  currentProgressItems.value = []
  currentTurnId.value = 0
  currentRunMode.value = 'new'
  rerunSourceTurnId.value = 0
}

const finalizeCurrentConversation = () => {
  if (!currentQuestion.value) return
  if (currentRunMode.value === 'rerun') {
    resetCurrentConversationState()
    scrollToBottom()
    return
  }
  const finalReply = currentReport.value || currentAssistantMessage.value
  chatHistory.value.push({
    turnId: currentTurnId.value,
    question: currentQuestion.value,
    chartArtifactId: currentChartArtifactId.value,
    charts: [...currentCharts.value],
    tables: [...currentTables.value],
    report: finalReply,
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
  chartArtifactId: item.chart_artifact_id || 0,
  charts: normalizeCharts(item.charts || []),
  tables: item.tables || [],
  report: item.report,
  progressItems: []
})

const fetchConversations = async () => {
  if (!activeNamespace.value) {
    conversations.value = []
    return
  }
  try {
    const response = await listConversations(activeNamespace.value)
    if (response.data.success) conversations.value = response.data.data || []
  } catch (error) {
    console.error('List conversations error:', error)
  }
}

const fetchCollects = async () => {
  if (!activeNamespace.value) {
    collects.value = []
    return
  }
  try {
    const response = await listCollects(activeNamespace.value)
    if (response.data.success) collects.value = response.data.data || []
  } catch (error) {
    console.error('List collects error:', error)
  }
}

const fetchSpaces = async () => {
  try {
    const response = await listNamespaces()
    if (response.data.success) {
      spaces.value = (response.data.data || []).map((item) => ({
        ...item,
        id: String(item.id)
      }))
    }
  } catch (error) {
    console.error('List namespaces error:', error)
    ElMessage.error('加载洞察空间失败')
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

const resolveChartArtifactForTurn = async (turnId) => {
  if (!activeConversationId.value || !turnId) return null
  const response = await getTurnDetail(activeConversationId.value, turnId)
  if (!response.data?.success) return null
  const detail = response.data.data
  const chartArtifact = (detail?.artifacts || []).find((artifact) => artifact.artifact_type === 'chart')
  if (!chartArtifact) return null
  return {
    artifactId: chartArtifact.id,
    title: chartArtifact.title || '',
    summaryText: chartArtifact.summary_text || '',
    chartSpec: chartArtifact.content_json?.chart_spec || null,
  }
}

const toggleChartCollectForTurn = async (item) => {
  const primaryChart = getPrimaryChart(item)
  if (!item?.turnId || !primaryChart) return
  let artifactId = item.chartArtifactId || 0
  let title = `${item.question} 图表`
  let summaryText = ''
  let chartSpec = primaryChart.chartSpec || null

  if (!artifactId) {
    try {
      const chartArtifact = await resolveChartArtifactForTurn(item.turnId)
      if (!chartArtifact) {
        ElMessage.warning('当前轮次未找到可收藏的图表产物')
        return
      }
      artifactId = chartArtifact.artifactId
      title = chartArtifact.title || title
      summaryText = chartArtifact.summaryText || ''
      chartSpec = chartArtifact.chartSpec || chartSpec
      item.chartArtifactId = artifactId
    } catch (error) {
      console.error('Resolve chart artifact error:', error)
      ElMessage.error('获取图表产物信息失败')
      return
    }
  }

  await toggleCollect({
    collectType: 'artifact',
    targetId: artifactId,
    title,
    summaryText,
    conversationId: activeConversationId.value,
    artifactId,
    metadata: {
      turn_id: item.turnId,
      chart_spec: chartSpec
    }
  })
}

const toggleCurrentChartCollect = async () => {
  const primaryChart = getPrimaryChart({ charts: currentCharts.value })
  if (!currentTurnId.value || !primaryChart) return
  let artifactId = currentChartArtifactId.value || 0
  let title = `${currentQuestion.value} 图表`
  let summaryText = ''
  let chartSpec = primaryChart.chartSpec || null

  if (!artifactId) {
    try {
      const chartArtifact = await resolveChartArtifactForTurn(currentTurnId.value)
      if (!chartArtifact) {
        ElMessage.warning('当前轮次未找到可收藏的图表产物')
        return
      }
      artifactId = chartArtifact.artifactId
      title = chartArtifact.title || title
      summaryText = chartArtifact.summaryText || ''
      chartSpec = chartArtifact.chartSpec || chartSpec
      currentChartArtifactId.value = artifactId
    } catch (error) {
      console.error('Resolve current chart artifact error:', error)
      ElMessage.error('获取图表产物信息失败')
      return
    }
  }

  await toggleCollect({
    collectType: 'artifact',
    targetId: artifactId,
    title,
    summaryText,
    conversationId: activeConversationId.value,
    artifactId,
    metadata: {
      turn_id: currentTurnId.value,
      chart_spec: chartSpec
    }
  })
}

const onDownloadChart = async ({ chart, title }) => {
  try {
    await downloadChartAsImage(chart, buildChartDownloadFilename(title))
    ElMessage.success('图表图片已开始下载')
  } catch (error) {
    console.error('Download chart error:', error)
    ElMessage.error(error?.message || '下载图表失败')
  }
}

const downloadBlobResponse = (response, fallbackName = 'analysis-result.pdf') => {
  const blob = response?.data
  if (!blob) {
    throw new Error('导出结果为空')
  }

  const disposition = response.headers?.['content-disposition'] || ''
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  const plainMatch = disposition.match(/filename="?([^"]+)"?/i)
  const filename = utf8Match
    ? decodeURIComponent(utf8Match[1])
    : (plainMatch?.[1] || fallbackName)

  const objectUrl = window.URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  window.URL.revokeObjectURL(objectUrl)
}

const onExportAnalysisPdf = async ({ turnId }) => {
  if (!activeConversationId.value || !turnId) return
  try {
    const response = await exportTurnPdf(activeConversationId.value, turnId)
    downloadBlobResponse(response, 'analysis-result.pdf')
    ElMessage.success('分析结果 PDF 已开始导出')
  } catch (error) {
    console.error('Export analysis pdf error:', error)
    ElMessage.error(error?.message || '导出 PDF 失败')
  }
}

const onRerunTurn = (item) => {
  if (!activeConversationId.value || !item?.turnId) return
  stopCurrentStream()
  resetCurrentConversationState()
  currentRunMode.value = 'rerun'
  rerunSourceTurnId.value = item.turnId
  currentTurnId.value = item.turnId
  currentQuestion.value = item.question
  currentReport.value = ''
  currentChartArtifactId.value = 0
  loading.value = true
  addProgressItem({
    type: 'status',
    level: 'info',
    message: `正在重新执行第 ${item.turnId} 轮分析...`
  })
  scrollToBottom()

  currentStreamController.value = streamRerunTurn(
    activeConversationId.value,
    item.turnId,
    handleStreamEvent,
    handleStreamError,
    handleStreamDone
  )
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

const clearActiveConversationView = () => {
  activeConversationId.value = 0
  chatHistory.value = []
  loading.value = false
  resetCurrentConversationState()
  turnDetailVisible.value = false
  turnDetail.value = null
  previewArtifact.value = null
}

const onDeleteConversation = async (conversation) => {
  if (!conversation?.id) return
  try {
    await ElMessageBox.confirm(
      `确认删除会话“${conversation.title || `会话 #${conversation.id}`}”吗？删除后会同步清理该会话的历史、分析结果、绑定关系和收藏。`,
      '删除会话',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
    const deletingCurrent = activeConversationId.value === conversation.id
    if (deletingCurrent) stopCurrentStream()
    await deleteConversation(conversation.id)
    await Promise.all([fetchConversations(), fetchCollects()])

    if (deletingCurrent) {
      const nextConversation = conversations.value[0]
      if (nextConversation?.id) {
        await loadConversationHistory(nextConversation.id)
      } else {
        clearActiveConversationView()
      }
    }
    ElMessage.success('会话已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Delete conversation error:', error)
      ElMessage.error(error?.response?.data?.message || '删除会话失败')
    }
  }
}

const previewChartArtifact = (artifact) => {
  if (artifact?.artifact_type === 'chart' && artifactChartSpec(artifact)) {
    previewArtifact.value = artifact
  }
}

const onSelectSpace = async (space) => {
  stopCurrentStream()
  loading.value = false
  activeNamespace.value = String(space.id)
  activeConversationId.value = 0
  chatHistory.value = []
  resetCurrentConversationState()
  turnDetailVisible.value = false
  turnDetail.value = null
  previewArtifact.value = null
  await Promise.all([fetchConversations(), fetchCollects()])
  if (conversations.value.length > 0) {
    await loadConversationHistory(conversations.value[0].id)
  }
}

const onSelectConversation = async (conversation) => {
  if (!conversation?.id) return
  stopCurrentStream()
  loading.value = false
  chatHistory.value = []
  resetCurrentConversationState()
  turnDetailVisible.value = false
  turnDetail.value = null
  previewArtifact.value = null
  await loadConversationHistory(conversation.id)
}

const onNewConversation = async () => {
  if (!activeNamespace.value) {
    ElMessage.warning('请先选择一个洞察空间')
    return
  }
  stopCurrentStream()
  try {
    const response = await createConversation(activeNamespace.value)
    if (!response.data?.success) {
      ElMessage.error(response.data?.message || '新增会话失败')
      return
    }
    const conversation = response.data?.data
    await Promise.all([fetchConversations(), fetchCollects()])
    if (conversation?.id) {
      chatHistory.value = []
      resetCurrentConversationState()
      turnDetailVisible.value = false
      turnDetail.value = null
      previewArtifact.value = null
      await loadConversationHistory(conversation.id)
    }
    ElMessage.success('会话已创建')
  } catch (error) {
    console.error('Create conversation error:', error)
    ElMessage.error('新增会话失败')
  }
}

const onSelectCollect = async (collect) => {
  favoritesPanelVisible.value = false
  if (collect.insight_conversation_id) {
    await loadConversationHistory(collect.insight_conversation_id)
  }
  if (collect.collect_type === 'turn' && collect.target_id) {
    await openTurnDetail(collect.target_id)
    return
  }
  if (collect.collect_type !== 'artifact') return
  const metadata = parseCollectMetadata(collect)
  if (metadata.turn_id) {
    await openTurnDetail(metadata.turn_id)
  }
  if (metadata.chart_spec) {
    previewArtifact.value = {
      title: collect.title || '图表预览',
      artifact_type: 'chart',
      content_json: {
        chart_spec: metadata.chart_spec
      }
    }
    return
  }
}

const onNewSpace = async () => {
  stopCurrentStream()
  try {
    const { value } = await ElMessageBox.prompt('请输入新的洞察空间名称', '新建洞察', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputPattern: /.*\S.*/,
      inputErrorMessage: '空间名称不能为空'
    })
    const response = await createNamespace(value)
    if (!response.data.success) {
      ElMessage.error(response.data.message || '创建洞察空间失败')
      return
    }
    const namespace = response.data.data?.namespace
    const conversation = response.data.data?.conversation
    await fetchSpaces()
    activeNamespace.value = namespace ? String(namespace.id) : ''
    conversations.value = conversation ? [conversation] : []
    activeConversationId.value = conversation?.id || 0
    chatHistory.value = []
    loading.value = false
    resetCurrentConversationState()
    turnDetailVisible.value = false
    turnDetail.value = null
    previewArtifact.value = null
    await fetchCollects()
    ElMessage.success('洞察空间已创建')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Create namespace error:', error)
      ElMessage.error('创建洞察空间失败')
    }
  }
}

const onDeleteSpace = async (space) => {
  if (!space?.id) return
  try {
    await ElMessageBox.confirm(
      `删除洞察空间“${space.name}”后，会同步删除该空间下的会话，是否继续？`,
      '删除洞察空间',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
    const deletingCurrent = activeNamespace.value === String(space.id)
    await deleteNamespace(space.id)
    await fetchSpaces()
    if (deletingCurrent) {
      activeConversationId.value = 0
      chatHistory.value = []
      resetCurrentConversationState()
      turnDetailVisible.value = false
      turnDetail.value = null
      previewArtifact.value = null
      if (spaces.value.length > 0) {
        await onSelectSpace(spaces.value[0])
      } else {
        activeNamespace.value = ''
        conversations.value = []
        collects.value = []
      }
    } else {
      await Promise.all([fetchConversations(), fetchCollects()])
    }
    ElMessage.success('洞察空间已删除')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Delete namespace error:', error)
      ElMessage.error('删除洞察空间失败')
    }
  }
}

const onRenameSpace = async (space) => {
  if (!space?.id) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的洞察空间名称', '重命名洞察空间', {
      inputValue: space.name || '',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputPattern: /.*\S.*/,
      inputErrorMessage: '空间名称不能为空'
    })
    const response = await renameNamespace(space.id, value)
    if (!response.data.success) {
      ElMessage.error(response.data.message || '重命名洞察空间失败')
      return
    }
    await fetchSpaces()
    if (activeNamespace.value === String(space.id)) {
      activeNamespace.value = String(space.id)
    }
    ElMessage.success('洞察空间名称已更新')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      console.error('Rename namespace error:', error)
      ElMessage.error('重命名洞察空间失败')
    }
  }
}

const onDataSourceChange = (dataSource) => {
  currentDataSource.value = dataSource
}

const finalizeStreamRound = async () => {
  const rerunConversationId = activeConversationId.value
  const isRerun = currentRunMode.value === 'rerun'
  loading.value = false
  currentStreamController.value = null
  finalizeCurrentConversation()
  await Promise.all([fetchConversations(), fetchCollects()])
  if (isRerun && rerunConversationId) {
    await loadConversationHistory(rerunConversationId)
  }
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
      currentCharts.value = normalizeCharts(event.charts || [])
      if (Array.isArray(event.tables)) currentTables.value = event.tables
      if (event.chart_artifact_id) currentChartArtifactId.value = Number(event.chart_artifact_id)
      if (event.analysis_report) currentReport.value = event.analysis_report
      scrollToBottom()
      return
  }
  if (event.type === 'done') {
    if (!currentReport.value && currentAssistantMessage.value) {
      currentReport.value = currentAssistantMessage.value
    } else if (!currentReport.value && currentCharts.value.length === 0) {
      currentReport.value = '本轮分析未生成可展示的图表或分析报告，请重试。'
    }
    await finalizeStreamRound()
    return
  }
  if (event.type === 'error') {
    addProgressItem(event)
    if (!currentReport.value) currentReport.value = event.message || '分析过程中发生错误。'
    await finalizeStreamRound()
    return
  }
  if (['status', 'assistant', 'tool_log', 'message'].includes(event.type)) {
    if (event.type === 'assistant' && event.message) {
      if (isInternalAssistantMessage(event.message)) return
      currentAssistantMessage.value = event.message
    }
    addProgressItem(event)
  }
}

const handleStreamError = async (error) => {
  console.error('Agent stream error:', error)
  ElMessage.error(`请求失败: ${error.message || '未知错误'}`)
  addProgressItem({ type: 'error', level: 'error', message: '请求失败，无法获取实时分析结果。' })
  if (!currentReport.value) currentReport.value = '请求失败了。'
  await finalizeStreamRound()
}

const handleStreamDone = async () => {
  if (!loading.value) return
  if (!currentReport.value && currentAssistantMessage.value) {
    currentReport.value = currentAssistantMessage.value
  } else if (!currentReport.value && currentCharts.value.length === 0) {
    currentReport.value = '本轮分析未生成可展示的图表或分析报告，请重试。'
  }
  await finalizeStreamRound()
}

const onSendMessage = (content) => {
  if (!content.trim()) return
  if (!activeNamespace.value) {
    ElMessage.warning('请先创建或选择一个洞察空间')
    return
  }
  stopCurrentStream()
  resetCurrentConversationState()
  currentRunMode.value = 'new'
  currentQuestion.value = content
  loading.value = true
  addProgressItem({ type: 'status', level: 'info', message: '正在建立分析会话并加载上下文...' })
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

const useQuickPrompt = (prompt) => {
  onSendMessage(prompt)
}

const showHelp = () => {
  ElMessage.info('当前页面已切换为新版原型布局，帮助和设置面板后续再细化。')
}

const showKnowledgePlaceholder = () => {
  ElMessage.info('知识库区域已预留，后续按统一知识资源接口接入。')
}

onMounted(async () => {
  await fetchSpaces()
  if (spaces.value.length > 0) {
    await onSelectSpace(spaces.value[0])
    return
  }
  activeNamespace.value = ''
  activeConversationId.value = 0
  conversations.value = []
  collects.value = []
  chatHistory.value = []
  resetCurrentConversationState()
})
</script>

<style scoped>
:global(body) { margin: 0; background: #eef3f9; color: #0f172a; }
.app-shell { min-height: 100vh; background: linear-gradient(180deg, #f3f7fc 0%, #edf3fa 100%); }
.top-nav { position: fixed; inset: 0 0 auto 0; z-index: 30; height: 56px; padding: 0 20px; display: flex; align-items: center; justify-content: space-between; background: rgba(11, 18, 32, 0.94); color: #fff; }
.logo { display: flex; align-items: center; gap: 12px; }
.logo-icon { width: 36px; height: 36px; border-radius: 12px; background: linear-gradient(135deg, #2563eb, #0ea5e9); display: inline-flex; align-items: center; justify-content: center; font-weight: 800; }
.logo-title { font-size: 15px; font-weight: 700; }
.logo-subtitle { margin-top: 2px; font-size: 11px; color: rgba(255, 255, 255, 0.64); }
.nav-actions { display: flex; align-items: center; gap: 12px; }
.nav-icon { position: relative; width: 34px; height: 34px; border: none; border-radius: 12px; background: rgba(255, 255, 255, 0.08); color: #fff; cursor: pointer; }
.nav-icon:hover { background: rgba(255, 255, 255, 0.14); }
.badge { position: absolute; top: -6px; right: -6px; min-width: 18px; height: 18px; padding: 0 6px; border-radius: 999px; background: #ef4444; display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; }
.user-avatar { width: 36px; height: 36px; border-radius: 50%; background: linear-gradient(135deg, #6366f1, #8b5cf6); display: inline-flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; }
.main-container { display: flex; height: calc(100vh - 56px); margin-top: 56px; }
.right-area { flex: 1; display: flex; overflow: hidden; }
.chat-panel { flex: 1; min-width: 420px; display: flex; flex-direction: column; background: linear-gradient(180deg, #f7fbff 0%, #eef5fb 100%); overflow: hidden; }
.chat-header { padding: 16px 20px; background: rgba(255,255,255,0.98); border-bottom: 1px solid #dbe3ef; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.chat-title { display: flex; align-items: center; gap: 12px; font-size: 16px; font-weight: 700; color: #0f172a; }
.chat-title-icon { width: 40px; height: 40px; border-radius: 14px; background: linear-gradient(135deg, #2563eb, #0ea5e9); display: inline-flex; align-items: center; justify-content: center; color: #fff; }
.chat-subtitle { margin-top: 4px; font-size: 12px; color: #64748b; font-weight: 500; }
.chat-header-actions, .result-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.action-btn { border: 1px solid #dbe3ef; background: #fff; border-radius: 10px; padding: 8px 12px; font-size: 12px; color: #475569; cursor: pointer; }
.chat-messages { flex: 1; overflow-y: auto; padding: 20px; background: radial-gradient(circle at top center, rgba(37, 99, 235, 0.08), transparent 24%), linear-gradient(180deg, #f7fbff 0%, #eef5fb 100%); }
.welcome-card { text-align: center; padding: 48px 24px; color: #1e293b; }
.welcome-icon { width: 84px; height: 84px; margin: 0 auto 20px; border-radius: 24px; background: linear-gradient(135deg, #2563eb, #0ea5e9); display: inline-flex; align-items: center; justify-content: center; font-size: 36px; }
.welcome-title { font-size: 26px; font-weight: 700; }
.welcome-subtitle { max-width: 560px; margin: 14px auto 0; line-height: 1.8; color: #64748b; font-size: 14px; }
.quick-actions { margin-top: 24px; display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; }
.quick-action { border: 1px solid #d7e4f3; background: #ffffff; color: #475569; border-radius: 999px; padding: 10px 16px; font-size: 13px; cursor: pointer; }
.quick-action:hover { border-color: #60a5fa; color: #1d4ed8; background: #eff6ff; }
.chat-message { margin-bottom: 18px; display: flex; gap: 12px; }
.chat-message.user { flex-direction: row-reverse; }
.message-avatar { width: 36px; height: 36px; border-radius: 12px; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 12px; font-weight: 700; }
.message-avatar.user { background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #fff; }
.message-avatar.ai { background: linear-gradient(135deg, #2563eb, #0ea5e9); color: #fff; }
.message-body { flex: 1; min-width: 0; }
.message-bubble { max-width: 72%; border-radius: 16px; padding: 14px 16px; line-height: 1.75; font-size: 14px; }
.message-bubble.user { margin-left: auto; background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; }
.message-bubble.ai { background: #f8fbff; color: #0f172a; }
.report-text :deep(p:first-child) { margin-top: 0; }
.progress-card, .result-card { border-radius: 18px; padding: 16px; background: #fff; border: 1px solid #dbe3ef; }
.progress-title, .result-title { font-size: 14px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }
.progress-list, .detail-list, .turn-detail, .detail-section { display: flex; flex-direction: column; gap: 12px; }
.progress-item { padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }
.progress-item.info { background: #eef5ff; color: #245b9f; }
.progress-item.success { background: #edf9f0; color: #2f7d4d; }
.progress-item.warning { background: #fff7e8; color: #9a6700; }
.progress-item.error { background: #fff0f0; color: #c45656; }
.progress-item.assistant { background: #f5f3ff; color: #5b45b0; }
.progress-empty { font-size: 12px; color: #64748b; }
.result-card { margin: 14px 0 18px 48px; }
.answer-card { width: 100%; margin: 0; }
.answer-chart { margin-bottom: 14px; }
.answer-report {
  max-width: 100%;
  width: 100%;
  box-sizing: border-box;
  padding: 4px 6px 2px;
  margin: 0;
  background: transparent;
  border-radius: 0;
}
.answer-report :deep(p),
.answer-report :deep(ul),
.answer-report :deep(ol),
.answer-report :deep(pre),
.answer-report :deep(blockquote) {
  margin: 0 0 12px;
}
.answer-report :deep(p:last-child),
.answer-report :deep(ul:last-child),
.answer-report :deep(ol:last-child),
.answer-report :deep(pre:last-child),
.answer-report :deep(blockquote:last-child) {
  margin-bottom: 0;
}
.answer-report :deep(h1),
.answer-report :deep(h2),
.answer-report :deep(h3),
.answer-report :deep(h4) {
  margin: 0 0 12px;
  color: #0f172a;
}
.result-header, .favorites-header, .detail-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }
.favorites-mask { position: fixed; inset: 56px 0 0 0; background: rgba(15, 23, 42, 0.28); z-index: 34; }
.favorites-panel { position: fixed; top: 56px; right: -400px; width: 380px; height: calc(100vh - 56px); background: #fff; border-left: 1px solid #dbe3ef; z-index: 35; transition: right 0.28s ease; display: flex; flex-direction: column; }
.favorites-panel.show { right: 0; }
.favorites-header { padding: 16px 20px; border-bottom: 1px solid #e5edf7; margin-bottom: 0; }
.favorites-header h3 { margin: 0; font-size: 16px; color: #0f172a; }
.favorites-tabs { display: flex; gap: 6px; }
.favorites-tab { border: none; background: transparent; border-radius: 999px; padding: 8px 12px; font-size: 12px; color: #64748b; cursor: pointer; }
.favorites-tab.active { background: #edf4ff; color: #1d4ed8; }
.favorites-close { border: none; background: transparent; color: #64748b; font-size: 18px; cursor: pointer; }
.favorites-content { flex: 1; overflow-y: auto; padding: 16px; }
.favorite-item { border: 1px solid #e5edf7; border-radius: 16px; padding: 14px; margin-bottom: 12px; background: #f8fbff; cursor: pointer; }
.favorite-item:hover { border-color: #60a5fa; transform: translateY(-2px); }
.favorite-item-turn { background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); }
.favorite-item-artifact { background: linear-gradient(180deg, #f8fbff 0%, #f3f8ff 100%); }
.favorite-header-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.favorite-title, .detail-title, .artifact-title { font-size: 14px; font-weight: 700; color: #0f172a; display: flex; align-items: center; gap: 8px; }
.favorite-type-icon { color: #f59e0b; }
.favorite-preview { background: #eef4ff; border-radius: 10px; padding: 10px; margin-bottom: 10px; min-height: 78px; display: flex; align-items: center; justify-content: center; }
.favorite-turn-chart { margin-bottom: 12px; }
.favorite-artifact-preview { min-height: 180px; padding: 12px; align-items: stretch; }
.favorite-result-report { width: 100%; max-width: 100%; box-sizing: border-box; margin-top: 0; }
.chart-placeholder { display: flex; align-items: flex-end; gap: 8px; height: 58px; }
.chart-placeholder .bar { width: 18px; border-radius: 4px 4px 0 0; background: linear-gradient(180deg, #60a5fa, #2563eb); display: inline-block; }
.favorite-preview-text { font-size: 12px; color: #64748b; line-height: 1.7; text-align: center; }
.favorite-summary, .artifact-summary { margin-top: 8px; font-size: 12px; line-height: 1.7; color: #64748b; }
.favorite-meta, .detail-meta, .artifact-link, .detail-empty { font-size: 12px; color: #64748b; }
.favorite-meta { display: flex; justify-content: space-between; margin-top: 10px; }
.favorites-empty { min-height: 240px; border: 1px dashed #dbe3ef; border-radius: 18px; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; color: #64748b; padding: 24px; }
.empty-icon { width: 56px; height: 56px; border-radius: 18px; background: #edf4ff; display: inline-flex; align-items: center; justify-content: center; color: #1d4ed8; font-size: 24px; margin-bottom: 14px; }
.section-title { font-size: 14px; font-weight: 700; color: #334155; }
.detail-card { padding: 14px 16px; border-radius: 16px; background: #f8fbff; color: #0f172a; line-height: 1.75; white-space: pre-wrap; border: 1px solid #e5edf7; }
.detail-list-item { display: flex; flex-direction: column; gap: 8px; }
.detail-list-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.detail-pill { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-size: 11px; background: #e2e8f0; color: #334155; }
.detail-pill.muted { background: #f1f5f9; color: #64748b; }
@media (max-width: 1440px) { .result-card { margin-left: 0; } }
</style>
