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
        <div class="model-switcher">
          <span class="model-label">模型</span>
          <el-select
            v-model="selectedLlmModelId"
            class="model-select"
            size="small"
            filterable
            :loading="llmModelsLoading"
            :disabled="llmModelsLoading || llmModels.length === 0"
            placeholder="选择模型"
            @change="onSelectLlmModel"
          >
            <el-option
              v-for="model in llmModels"
              :key="model.id"
              :label="getLlmModelLabel(model)"
              :value="model.id"
            />
          </el-select>
        </div>
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
        :insight-task-count="insightTasks.length"
        :active-conversation-id="activeConversationId"
        @select-space="onSelectSpace"
        @select-conversation="onSelectConversation"
        @delete-conversation="onDeleteConversation"
        @delete-space="onDeleteSpace"
        @rename-space="onRenameSpace"
        @new-space="onNewSpace"
        @new-conversation="onNewConversation"
        @open-favorites="favoritesPanelVisible = true"
        @open-insight-tasks="openInsightTasks"
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
                    <div class="progress-title">分析过程</div>
                    <div class="progress-list">
                      <div
                        v-for="progress in item.progressItems"
                        :key="progress.id"
                        class="progress-item"
                        :class="progress.level"
                      >
                        <strong v-if="progress.title">{{ progress.title }}</strong>
                        <span>{{ progress.message }}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div v-if="getDisplayCharts(item).length || getDisplayTables(item).length || item.report" class="chat-message ai">
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
                          :disabled="!canLocalRunTurn(item) || localRerunLoading"
                          :title="getLocalRunDisabledReason(item)"
                          @click="onLocalRerunTurn(item)"
                        >
                          {{ localRerunLoading && localRerunTurnId === item.turnId ? '本地执行中...' : '本地执行' }}
                        </button>
                        <button
                          class="action-btn"
                          type="button"
                          :disabled="!hasTaskRunnableExecution(item.latestExecution)"
                          :title="getTaskDisabledReason(item.latestExecution)"
                          @click="openCreateTask(item)"
                        >
                          加入任务
                        </button>
                        <button
                          class="action-btn"
                          type="button"
                          @click="toggleCollect({
                            collectType: 'turn',
                            targetId: item.turnId,
                            title: item.question,
                            summaryText: item.report,
                            conversationId: activeConversationId
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
                        <ChartDisplay
                          :chart-spec="chart.chartSpec"
                          @point-click="onChartEvidenceClick(item, chart, chartIndex, $event)"
                        />
                      </div>
                    </div>

                    <div v-if="getDisplayTables(item).length" class="answer-table-list">
                      <div
                        v-for="(table, tableIndex) in getDisplayTables(item)"
                        :key="`${item.turnId}-table-${table.id || tableIndex}`"
                        class="answer-table-card"
                      >
                        <div class="answer-table-title">{{ table.title || `表格 ${tableIndex + 1}` }}</div>
                        <div v-if="table.description || table.summary_text" class="answer-table-desc">
                          {{ table.description || table.summary_text }}
                        </div>
                        <div class="answer-table-scroll">
                          <table class="answer-table">
                            <thead>
                              <tr>
                                <th v-for="column in getTableColumns(table)" :key="column.field">
                                  {{ column.title || column.field }}
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              <tr v-for="(row, rowIndex) in getTableRows(table)" :key="rowIndex">
                                <td
                                  v-for="column in getTableColumns(table)"
                                  :key="`${rowIndex}-${column.field}`"
                                  :class="{ 'traceable-cell': hasTableEvidenceRef(table, row, column) }"
                                  @click="onTableEvidenceClick(item, table, tableIndex, row, rowIndex, column)"
                                >
                                  {{ formatTableCell(row[column.field]) }}
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </div>
                      </div>
                    </div>

                    <div
                      v-if="item.report"
                      class="message-bubble ai report-text answer-report"
                      v-html="renderTraceableReport(item.report, getReportEvidenceRefs(item))"
                      @click="onReportEvidenceClick(item, $event)"
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
                    <div class="progress-title">分析过程</div>
                    <div v-if="currentProgressItems.length" class="progress-list">
                      <div
                        v-for="progress in currentProgressItems"
                        :key="progress.id"
                        class="progress-item"
                        :class="progress.level"
                      >
                        <strong v-if="progress.title">{{ progress.title }}</strong>
                        <span>{{ progress.message }}</span>
                      </div>
                    </div>
                    <div v-else class="progress-empty">正在准备分析上下文...</div>
                  </div>
                </div>
              </div>

              <div v-if="currentClarification" class="chat-message ai">
                <div class="message-avatar ai">AI</div>
                <div class="message-body">
                  <div class="clarification-card">
                    <div class="clarification-title">
                      {{ clarificationItems.length > 1 ? '需要确认几项关键业务语义' : '需要确认一项关键业务语义' }}
                    </div>
                    <div
                      v-for="(item, itemIndex) in clarificationItems"
                      :key="item.item_id"
                      class="clarification-item"
                    >
                      <div class="clarification-question">
                        <span v-if="clarificationItems.length > 1">{{ itemIndex + 1 }}.</span>
                        {{ item.question }}
                      </div>
                      <div v-if="item.options?.length" class="clarification-options">
                        <button
                          v-for="option in item.options"
                          :key="option.id"
                          class="clarification-option"
                          :class="{ selected: clarificationDraft(item).option_id === option.id }"
                          type="button"
                          :disabled="clarificationSubmitting"
                          @click="selectClarificationOption(item, option)"
                        >
                          <strong>{{ option.label }}</strong>
                          <span v-if="option.description">{{ option.description }}</span>
                        </button>
                      </div>
                      <div v-if="item.allow_free_text" class="clarification-free-text">
                        <el-input
                          :model-value="clarificationDraft(item).answer_text"
                          :disabled="clarificationSubmitting"
                          placeholder="也可以直接补充你的口径"
                          @update:model-value="updateClarificationText(item, $event)"
                          @keyup.enter="submitClarificationAnswer"
                        />
                      </div>
                    </div>
                    <div class="clarification-actions">
                      <button class="action-btn" type="button" :disabled="clarificationSubmitting" @click="onStopAnalysis">停止分析</button>
                      <button
                        class="action-btn primary"
                        type="button"
                        :disabled="clarificationSubmitting || !clarificationReady"
                        @click="submitClarificationAnswer"
                      >
                        {{ clarificationSubmitting ? '正在继续...' : '继续分析' }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <div v-if="currentCharts.length || currentTables.length || currentReport" class="chat-message ai">
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
                            conversationId: activeConversationId
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
                        <ChartDisplay
                          :chart-spec="chart.chartSpec"
                          @point-click="onChartEvidenceClick({ turnId: currentTurnId }, chart, chartIndex, $event)"
                        />
                      </div>
                    </div>
                    <div v-if="currentTables.length" class="answer-table-list">
                      <div
                        v-for="(table, tableIndex) in currentTables"
                        :key="`${currentTurnId}-table-${table.id || tableIndex}`"
                        class="answer-table-card"
                      >
                        <div class="answer-table-title">{{ table.title || `表格 ${tableIndex + 1}` }}</div>
                        <div v-if="table.description || table.summary_text" class="answer-table-desc">
                          {{ table.description || table.summary_text }}
                        </div>
                        <div class="answer-table-scroll">
                          <table class="answer-table">
                            <thead>
                              <tr>
                                <th v-for="column in getTableColumns(table)" :key="column.field">
                                  {{ column.title || column.field }}
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              <tr v-for="(row, rowIndex) in getTableRows(table)" :key="rowIndex">
                                <td
                                  v-for="column in getTableColumns(table)"
                                  :key="`${rowIndex}-${column.field}`"
                                  :class="{ 'traceable-cell': hasTableEvidenceRef(table, row, column) }"
                                  @click="onTableEvidenceClick({ turnId: currentTurnId }, table, tableIndex, row, rowIndex, column)"
                                >
                                  {{ formatTableCell(row[column.field]) }}
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </div>
                      </div>
                    </div>
                    <div
                      v-if="currentReport"
                      class="message-bubble ai report-text answer-report"
                      v-html="renderTraceableReport(currentReport, currentReportEvidenceRefs)"
                      @click="onReportEvidenceClick({ turnId: currentTurnId, reportEvidenceRefs: currentReportEvidenceRefs }, $event)"
                    />
                  </div>
                </div>
              </div>
            </template>
          </div>

          <el-switch v-model="clarificationEnabled" active-text="允许轮内澄清" :disabled="loading || !!currentClarification" />
          <ChatInput :loading="loading && !currentClarification" @send="onSendMessage" @stop="onStopAnalysis" />
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

              <div v-if="favoriteCharts(collect).length" class="favorite-turn-charts">
                <div
                  v-for="(chart, chartIndex) in favoriteCharts(collect)"
                  :key="`favorite-turn-${collect.id}-chart-${chart.artifactId || chartIndex}`"
                  class="favorite-turn-chart"
                >
                  <ChartDisplay :chart-spec="chart.chartSpec" />
                </div>
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

    <el-drawer
      v-model="insightTaskPanelVisible"
      title="洞察任务"
      size="72%"
      destroy-on-close
      @opened="insightTaskPanelOpened = true"
      @closed="insightTaskPanelOpened = false"
    >
      <div class="task-workspace">
        <div class="task-list-pane">
          <div class="task-pane-header">
            <div>
              <div class="section-title">任务列表</div>
              <div class="detail-meta">{{ insightTasks.length }} 个可复用洞察任务</div>
            </div>
            <button class="action-btn" type="button" @click="fetchInsightTasks">刷新</button>
          </div>
          <div v-if="insightTasksLoading" class="detail-empty">正在加载任务...</div>
          <button
            v-for="task in insightTasks"
            v-else
            :key="task.task_id"
            class="task-card"
            :class="{ active: selectedInsightTask?.task_id === task.task_id }"
            type="button"
            @click="selectInsightTask(task)"
          >
            <div class="task-card-title-row">
              <span class="task-card-title">{{ task.name }}</span>
              <span class="detail-pill" :class="{ muted: task.status !== 'enabled' }">
                {{ task.status === 'enabled' ? '已启用' : '已暂停' }}
              </span>
            </div>
            <div class="task-card-schedule">{{ formatTaskSchedule(task.schedule) }}</div>
            <div class="task-card-meta">
              <span>运行 {{ task.run_count || 0 }} 次</span>
              <span>{{ formatTaskRunStatus(task.latest_run) }}</span>
              <span>{{ task.next_run_at ? `下次 ${formatDateTime(task.next_run_at)}` : '无自动计划' }}</span>
            </div>
          </button>
          <div v-if="!insightTasksLoading && !insightTasks.length" class="favorites-empty task-empty">
            <p>还没有洞察任务</p>
            <span>在成功分析结果旁点击“加入任务”。</span>
          </div>
        </div>

        <div class="task-detail-pane">
          <div v-if="insightTaskDetailLoading" class="detail-empty">正在加载任务详情...</div>
          <template v-else-if="selectedInsightTask">
            <div class="task-detail-header">
              <div>
                <div class="detail-title">{{ selectedInsightTask.name }}</div>
                <div class="detail-meta">{{ selectedInsightTask.description || '暂无任务描述' }}</div>
              </div>
              <div class="detail-list-actions">
                <button class="action-btn" type="button" @click="selectInsightTask(selectedInsightTask)">刷新结果</button>
                <button class="action-btn" type="button" @click="openEditInsightTask(selectedInsightTask)">编辑</button>
                <button
                  class="action-btn"
                  type="button"
                  :disabled="insightTaskRunSubmitting"
                  @click="runSelectedInsightTask"
                >
                  {{ insightTaskRunSubmitting ? '执行中...' : '立即执行' }}
                </button>
                <button class="action-btn" type="button" @click="toggleSelectedTaskStatus">
                  {{ selectedInsightTask.status === 'enabled' ? '暂停' : '恢复' }}
                </button>
                <button class="action-btn" type="button" @click="removeSelectedInsightTask">删除</button>
              </div>
            </div>

            <div class="task-detail-sections">
              <details class="task-detail-section task-caliber-section">
                <summary class="task-caliber-head">
                  <div class="section-title">统计口径</div>
                  <span class="task-caliber-meta">结构化 · 可复用</span>
                </summary>
                <div class="task-caliber-grid">
                  <div
                    v-for="item in getTaskStatisticalCaliber(selectedInsightTask, selectedInsightTaskRun)"
                    :key="item.key"
                    class="task-caliber-item"
                    :class="{ wide: item.wide }"
                  >
                    <label>{{ item.label }}</label>
                    <span>{{ item.value }}</span>
                  </div>
                </div>
              </details>

              <section class="task-detail-section">
                <div class="task-section-heading">
                  <div>
                    <div class="section-title">分析报告</div>
                    <div class="detail-meta">
                      {{ selectedInsightTaskRun?.finished_at ? `结果时间：${formatDateTime(selectedInsightTaskRun.finished_at)}` : '选择成功执行记录后查看结果' }}
                    </div>
                  </div>
                  <span v-if="selectedInsightTaskRun?.status" class="detail-pill" :class="{ muted: selectedInsightTaskRun.status !== 'success' }">
                    {{ selectedInsightTaskRun.status }}
                  </span>
                </div>
                <div v-if="insightTaskPanelOpened && getTaskRunCharts(selectedInsightTaskRun).length" class="answer-chart-grid">
                  <div v-for="(chart, index) in getTaskRunCharts(selectedInsightTaskRun)" :key="`task-chart-${index}`" class="answer-chart">
                    <ChartDisplay :chart-spec="chart.chart_spec || chart.chartSpec || {}" />
                  </div>
                </div>
                <div v-if="getTaskRunTables(selectedInsightTaskRun).length" class="answer-table-list">
                  <div v-for="(table, index) in getTaskRunTables(selectedInsightTaskRun)" :key="`task-table-${index}`" class="answer-table-card">
                    <div class="answer-table-title">{{ table.title || `表格 ${index + 1}` }}</div>
                    <div class="answer-table-scroll">
                      <table class="answer-table">
                        <thead><tr><th v-for="column in getTableColumns(table)" :key="column.field">{{ column.title || column.field }}</th></tr></thead>
                        <tbody><tr v-for="(row, rowIndex) in table.rows || []" :key="rowIndex"><td v-for="column in getTableColumns(table)" :key="column.field">{{ row[column.field] }}</td></tr></tbody>
                      </table>
                    </div>
                  </div>
                </div>
                <div v-if="selectedInsightTaskRun?.analysis_report" class="detail-card answer-report" v-html="renderMarkdown(selectedInsightTaskRun.analysis_report)" />
                <div v-else class="detail-empty">选择一条成功执行记录后查看报告。</div>
              </section>

              <section class="task-detail-section">
                <div class="task-section-heading">
                  <div>
                    <div class="section-title">执行记录</div>
                    <div class="detail-meta">共 {{ selectedInsightTask.run_count || 0 }} 次执行，点击记录可查看对应结果</div>
                  </div>
                </div>
                <div class="task-run-list">
                  <button
                    v-for="run in selectedInsightTask.runs || []"
                    :key="run.run_id"
                    class="task-run-item"
                    :class="{ active: selectedInsightTaskRun?.run_id === run.run_id }"
                    type="button"
                    @click="selectInsightTaskRun(run)"
                  >
                    <span>{{ formatTaskRunTrigger(run.trigger_type) }}</span>
                    <span class="detail-pill" :class="{ muted: run.status !== 'success' }">{{ run.status }}</span>
                    <span>{{ formatDateTime(run.scheduled_for) }}</span>
                    <span>{{ run.execution_seconds ? `${(run.execution_seconds / 1000).toFixed(1)}s` : '-' }}</span>
                  </button>
                  <div v-if="!(selectedInsightTask.runs || []).length" class="detail-empty">暂无执行记录。</div>
                  <button
                    v-if="Number(selectedInsightTask.run_count || 0) > (selectedInsightTask.runs || []).length"
                    class="action-btn task-load-more"
                    type="button"
                    :disabled="insightTaskRunsLoading"
                    @click="loadMoreInsightTaskRuns"
                  >
                    {{ insightTaskRunsLoading ? '加载中...' : '加载更多执行记录' }}
                  </button>
                </div>
                <div v-if="selectedInsightTaskRun?.error_message" class="task-run-error">
                  {{ selectedInsightTaskRun.error_message }}
                </div>
              </section>
            </div>
          </template>
          <div v-else class="detail-empty">从左侧选择一个洞察任务。</div>
        </div>
      </div>
    </el-drawer>

    <el-dialog
      v-model="insightTaskEditorVisible"
      :title="insightTaskEditorMode === 'edit' ? '编辑洞察任务' : '加入洞察任务'"
      width="520px"
      destroy-on-close
    >
      <el-form label-position="top">
        <el-form-item label="任务名称">
          <el-input v-model="insightTaskEditorForm.name" maxlength="260" show-word-limit />
        </el-form-item>
        <el-form-item label="任务描述">
          <el-input v-model="insightTaskEditorForm.description" type="textarea" :rows="3" maxlength="1000" show-word-limit />
        </el-form-item>
        <el-form-item label="任务频率">
          <el-select v-model="insightTaskEditorForm.frequency" style="width: 100%">
            <el-option label="每天" value="daily" />
            <el-option label="工作日" value="weekdays" />
            <el-option label="每周" value="weekly" />
            <el-option label="每月" value="monthly" />
          </el-select>
        </el-form-item>
        <el-form-item label="执行时间">
          <el-time-picker v-model="insightTaskEditorForm.time" format="HH:mm" value-format="HH:mm" style="width: 100%" />
        </el-form-item>
        <el-form-item v-if="insightTaskEditorForm.frequency === 'weekly'" label="执行星期">
          <el-checkbox-group v-model="insightTaskEditorForm.weekdays">
            <el-checkbox v-for="item in weekdayOptions" :key="item.value" :value="item.value">{{ item.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item v-if="insightTaskEditorForm.frequency === 'monthly'" label="执行日期">
          <el-input-number v-model="insightTaskEditorForm.dayOfMonth" :min="1" :max="31" controls-position="right" />
          <span class="task-schedule-hint">当月没有该日期时，在当月最后一天执行</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <button class="action-btn" type="button" @click="insightTaskEditorVisible = false">取消</button>
        <el-button type="primary" :loading="insightTaskEditorLoading" @click="submitInsightTaskEditor">
          {{ insightTaskEditorMode === 'edit' ? '保存修改' : '创建任务' }}
        </el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="turnDetailVisible" title="轮次详情" size="48%" destroy-on-close>
      <div v-if="turnDetailLoading" class="detail-empty">正在加载详情...</div>
      <div v-else-if="turnDetail" class="turn-detail">
        <div class="detail-header">
          <div>
            <div class="detail-title">第 {{ turnDetail.turn.turn_no }} 轮分析</div>
            <div class="detail-meta">状态：{{ turnDetail.turn.status }} | 开始时间：{{ formatDateTime(turnDetail.turn.started_at) }}</div>
          </div>
          <div class="detail-list-actions">
            <button
              class="action-btn"
              type="button"
              :disabled="!canLocalRunTurn(getTurnDetailLocalRunItem()) || localRerunLoading"
              :title="getLocalRunDisabledReason(getTurnDetailLocalRunItem())"
              @click="onLocalRerunTurn(getTurnDetailLocalRunItem())"
            >
              {{ localRerunLoading && localRerunTurnId === turnDetail.turn.id ? '本地执行中...' : '本地执行' }}
            </button>
            <button
              class="action-btn"
              type="button"
              :disabled="!hasTaskRunnableExecution(turnDetail.latest_execution)"
              :title="getTaskDisabledReason(turnDetail.latest_execution)"
              @click="openCreateTask(getTurnDetailLocalRunItem())"
            >
              加入任务
            </button>
            <button
              class="action-btn"
              type="button"
              @click="toggleCollect({
                collectType: 'turn',
                targetId: turnDetail.turn.id,
                title: turnDetail.turn.user_query,
                summaryText: turnDetail.turn.final_answer,
                conversationId: turnDetail.conversation.id
              })"
            >
              {{ isCollected('turn', turnDetail.turn.id) ? '取消收藏' : '收藏本轮' }}
            </button>
          </div>
        </div>

        <el-tabs v-model="turnDetailActiveTab" class="detail-tabs" @tab-change="onTurnDetailTabChange">
          <el-tab-pane label="当前结果" name="current">
            <div class="detail-section">
              <div class="section-title">问题</div>
              <div class="detail-card">{{ turnDetail.turn.user_query }}</div>
            </div>

            <div v-if="turnDetail.turn.final_answer" class="detail-section">
              <div class="section-title">最终结论</div>
              <div
                class="detail-card answer-report"
                v-html="renderTraceableReport(turnDetail.turn.final_answer, getTurnDetailReportEvidenceRefs(turnDetail))"
                @click="onReportEvidenceClick({ turnId: turnDetail.turn.id, reportEvidenceRefs: getTurnDetailReportEvidenceRefs(turnDetail) }, $event)"
              />
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
                        artifactId: artifact.id
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
                <ChartDisplay
                  :chart-spec="artifactChartSpec(previewArtifact)"
                  @point-click="onArtifactChartEvidenceClick(previewArtifact, $event)"
                />
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="分析历史" name="versions">
            <div v-if="turnVersionsLoading" class="detail-empty version-empty">正在加载分析历史...</div>
            <div v-else-if="!turnVersions.length" class="detail-empty version-empty">当前轮次还没有分析历史。</div>
            <div v-else class="version-layout">
              <div class="version-list">
                <button
                  v-for="version in turnVersions"
                  :key="getTurnVersionKey(version)"
                  class="version-item"
                  :class="{ active: selectedVersionKey === getTurnVersionKey(version), current: version.is_current }"
                  type="button"
                  @click="selectTurnVersion(version)"
                >
                  <div class="version-item-main">
                    <span>{{ getVersionLabel(version) }}</span>
                    <span v-if="version.is_current" class="detail-pill">当前</span>
                  </div>
                  <div class="version-item-meta">
                    <span>{{ version.status || 'unknown' }}</span>
                    <span>{{ formatDateTime(version.finished_at || version.started_at || version.created_at) }}</span>
                  </div>
                  <div class="version-item-meta">
                    <span>执行 {{ version.execution_count || 0 }}</span>
                    <span>产物 {{ version.artifact_count || 0 }}</span>
                  </div>
                </button>
              </div>

              <div class="version-preview">
                <div v-if="turnVersionDetailLoading" class="detail-empty version-empty">正在加载版本产物...</div>
                <div v-else-if="selectedVersionDetail" class="version-content">
                  <div class="detail-list-actions version-summary">
                    <span class="detail-pill">{{ selectedVersionDetail.version?.status || 'unknown' }}</span>
                    <span class="detail-pill muted">{{ getVersionLabel(selectedVersionDetail.version || {}) }}</span>
                    <span class="detail-meta">{{ formatDateTime(selectedVersionDetail.version?.finished_at || selectedVersionDetail.version?.started_at || selectedVersionDetail.version?.created_at) }}</span>
                  </div>

                  <div v-if="hasObjectEntries(getVersionParameters(selectedVersionDetail))" class="detail-section">
                    <div class="section-title">执行参数</div>
                    <pre class="detail-card version-json">{{ formatJson(getVersionParameters(selectedVersionDetail)) }}</pre>
                  </div>

                  <div v-if="getVersionCharts(selectedVersionDetail).length" class="detail-section">
                    <div class="section-title">图表</div>
                    <div class="answer-chart-grid">
                      <div
                        v-for="(chart, chartIndex) in getVersionCharts(selectedVersionDetail)"
                        :key="`version-chart-${chart.id || chartIndex}`"
                        class="answer-chart-card"
                      >
                        <div class="answer-chart-title">{{ chart.title || `图表 ${chartIndex + 1}` }}</div>
                        <div v-if="chart.summaryText" class="answer-chart-desc">{{ chart.summaryText }}</div>
                        <ChartDisplay
                          :chart-spec="chart.chartSpec"
                          @point-click="onChartEvidenceClick({ turnId: selectedVersionDetail?.turn?.id || turnDetail?.turn?.id }, chart, chartIndex, $event)"
                        />
                      </div>
                    </div>
                  </div>

                  <div v-if="getVersionTables(selectedVersionDetail).length" class="detail-section">
                    <div class="section-title">表格</div>
                    <div class="answer-table-list">
                      <div
                        v-for="(table, tableIndex) in getVersionTables(selectedVersionDetail)"
                        :key="`version-table-${table.id || tableIndex}`"
                        class="answer-table-card"
                      >
                        <div class="answer-table-title">{{ table.title || `表格 ${tableIndex + 1}` }}</div>
                        <div v-if="table.description || table.summary_text" class="answer-table-desc">
                          {{ table.description || table.summary_text }}
                        </div>
                        <div class="answer-table-scroll">
                          <table class="answer-table">
                            <thead>
                              <tr>
                                <th v-for="column in getTableColumns(table)" :key="column.field">
                                  {{ column.title || column.field }}
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              <tr v-for="(row, rowIndex) in getTableRows(table)" :key="rowIndex">
                                <td
                                  v-for="column in getTableColumns(table)"
                                  :key="`${rowIndex}-${column.field}`"
                                  :class="{ 'traceable-cell': hasTableEvidenceRef(table, row, column) }"
                                  @click="onTableEvidenceClick({ turnId: selectedVersionDetail?.turn?.id || turnDetail?.turn?.id }, table, tableIndex, row, rowIndex, column)"
                                >
                                  {{ formatTableCell(row[column.field]) }}
                                </td>
                              </tr>
                            </tbody>
                          </table>
                        </div>
                      </div>
                    </div>
                  </div>

                  <div v-if="getVersionReport(selectedVersionDetail)" class="detail-section">
                    <div class="section-title">报告</div>
                    <div
                      class="detail-card answer-report"
                      v-html="renderTraceableReport(getVersionReport(selectedVersionDetail), getVersionReportEvidenceRefs(selectedVersionDetail))"
                      @click="onReportEvidenceClick({ turnId: selectedVersionDetail?.turn?.id || turnDetail?.turn?.id, reportEvidenceRefs: getVersionReportEvidenceRefs(selectedVersionDetail) }, $event)"
                    />
                  </div>

                  <div
                    v-if="!getVersionReport(selectedVersionDetail) && !getVersionCharts(selectedVersionDetail).length && !getVersionTables(selectedVersionDetail).length"
                    class="detail-empty version-empty"
                  >
                    当前版本没有可展示产物。
                  </div>
                </div>
                <div v-else class="detail-empty version-empty">请选择一个分析版本。</div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
      <div v-else class="detail-empty">没有可展示的轮次详情。</div>
    </el-drawer>

    <el-drawer v-model="evidenceDrawerVisible" title="" size="42%" destroy-on-close custom-class="evidence-drawer">
      <div v-if="evidenceLoading" class="detail-empty">正在加载证据链...</div>
      <div v-else-if="evidenceDetail" class="evidence-panel">
        <div class="evidence-hero">
          <div class="evidence-hero-title">{{ getEvidenceDisplayValue(evidenceDetail) }} 是怎么计算出来的？</div>
          <div class="evidence-hero-subtitle">{{ evidenceDetail.metric_name || '可追溯指标' }}</div>
        </div>
        <div class="evidence-chain">
          <div
            v-for="(step, stepIndex) in evidenceDetail.chain_steps || []"
            :key="`${step.type || 'step'}-${stepIndex}`"
            :class="['evidence-step', { 'evidence-step-final': stepIndex === (evidenceDetail.chain_steps || []).length - 1 }]"
          >
            <div class="evidence-step-rail">
              <div class="evidence-step-index">{{ stepIndex + 1 }}</div>
            </div>
            <div class="evidence-step-body">
              <div class="evidence-step-title">{{ step.title || step.type }}</div>
              <div class="evidence-step-summary">{{ step.summary || '该步骤未提供额外说明。' }}</div>
              <div v-if="stepItems(step).length" class="evidence-step-items">
                <div v-for="(item, itemIndex) in stepItems(step)" :key="`${step.type}-item-${itemIndex}`" class="evidence-step-item">
                  <span class="evidence-item-label">{{ item.label }}</span>
                  <span class="evidence-item-value">{{ formatEvidenceValue(item.value) }}</span>
                </div>
              </div>
              <div v-if="step.method" class="evidence-method">
                <div class="evidence-method-label">计算方法</div>
                <div class="evidence-method-value">{{ step.method }}</div>
              </div>
              <div v-if="step.formula" class="evidence-formula">{{ step.formula }}</div>
              <div v-if="step.output" class="evidence-output">{{ step.output }}</div>
              <div v-if="step.type !== 'result' && stepBadges(step).length" class="evidence-step-badges">
                <span v-for="badge in stepBadges(step)" :key="badge" class="evidence-badge">{{ badge }}</span>
              </div>
              <div v-if="stepWarnings(step).length" class="evidence-warnings">
                <div v-for="(warning, warningIndex) in stepWarnings(step)" :key="`${step.type}-warning-${warningIndex}`" class="evidence-warning">
                  {{ warning }}
                </div>
              </div>
              <div v-if="step.type === 'result' || step.type === 'calculation'" class="evidence-result-card">
                <div class="evidence-result-value">{{ formatEvidenceValue(step.result_value || getEvidenceDisplayValue(evidenceDetail)) }}</div>
                <div class="evidence-result-label">{{ evidenceDetail.metric_name || '最终结果' }}</div>
                <div v-if="stepBadges(step).length" class="evidence-result-badges">
                  <span v-for="badge in stepBadges(step)" :key="badge" class="evidence-badge">{{ badge }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <el-collapse v-if="hasEvidenceDebugInfo(evidenceDetail)" class="evidence-debug-collapse">
          <el-collapse-item title="查看 SQL" name="sql">
            <pre class="evidence-debug-pre">{{ getEvidenceSqlText(evidenceDetail) }}</pre>
          </el-collapse-item>
          <el-collapse-item title="查看执行代码" name="code">
            <pre class="evidence-debug-pre">{{ getEvidenceCodeText(evidenceDetail) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
      <div v-else class="detail-empty">没有可展示的证据链。</div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { marked } from 'marked'
import { ElMessage, ElMessageBox } from 'element-plus'

import Sidebar from './components/Sidebar.vue'
import DataSourcePanel from './components/DataSourcePanel.vue'
import ChatInput from './components/ChatInput.vue'
import ChartDisplay from './components/ChartDisplay.vue'
import { applyPieLegendSafety } from './utils/chartSpecSafety'
import {
  createConversation,
  createNamespace,
  createCollect,
  createInsightTask,
  cancelAnalysisTask,
  deleteConversation,
  deleteNamespace,
  exportTurnPdf,
  getInitialTurnVersion,
  getInsightTask,
  getInsightTaskRun,
  getConversationHistory,
  getTurnDetail,
  getTurnVersionDetail,
  listLlmModels,
  listCollects,
  listInsightTasks,
  listInsightTaskRuns,
  listConversations,
  listNamespaces,
  listTurnVersions,
  removeCollect,
  deleteInsightTask,
  renameConversation,
  renameNamespace,
  rerunExecutionLocally,
  runInsightTask,
  resolveEvidence,
  selectLlmModel,
  submitAgentTask,
  submitTaskClarification,
  submitRerunTurnTask,
  streamTurnEvents,
  getAnalysisTask,
  getRunningTurn,
  updateInsightTask
} from './api/agent.js'

marked.setOptions({ breaks: true, gfm: true })

const spaces = ref([])
const activeNamespace = ref('')
const conversations = ref([])
const collects = ref([])
const activeConversationId = ref(0)
const loading = ref(false)
const clarificationEnabled = ref(true)
const chatHistory = ref([])
const currentQuestion = ref('')
const currentChartArtifactId = ref(0)
const currentCharts = ref([])
const currentTables = ref([])
const currentReport = ref('')
const currentReportRefs = ref({})
const currentAssistantMessage = ref('')
const currentDataSource = ref(null)
const currentProgressItems = ref([])
const currentStreamController = ref(null)
const currentTaskId = ref('')
const currentTurnId = ref(0)
const currentRunMode = ref('new')
const currentClarification = ref(null)
const clarificationAnswers = ref({})
const clarificationSubmitting = ref(false)
const rerunSourceTurnId = ref(0)
const localRerunLoading = ref(false)
const localRerunTurnId = ref(0)
const messagesContainer = ref(null)
const turnDetailVisible = ref(false)
const turnDetailLoading = ref(false)
const turnDetail = ref(null)
const turnDetailActiveTab = ref('current')
const turnVersionsLoading = ref(false)
const turnVersions = ref([])
const selectedVersionKey = ref('')
const selectedVersionDetail = ref(null)

const clarificationItems = computed(() => {
  if (!currentClarification.value) return []
  const items = Array.isArray(currentClarification.value.items)
    ? currentClarification.value.items.filter(item => item && typeof item === 'object')
    : []
  const normalized = items.length ? items : [currentClarification.value]
  return normalized.map((item, index) => ({
    ...item,
    item_id: String(item.item_id || `item_${index + 1}`)
  }))
})

const clarificationDraft = (item) => clarificationAnswers.value[item.item_id] || {
  option_id: '',
  answer_text: ''
}

const clarificationReady = computed(() => (
  clarificationItems.value.length > 0
  && clarificationItems.value.every(item => clarificationDraft(item).answer_text.trim())
))

const selectClarificationOption = (item, option) => {
  clarificationAnswers.value = {
    ...clarificationAnswers.value,
    [item.item_id]: {
      option_id: String(option?.id || ''),
      answer_text: String(option?.label || option?.value || '').trim()
    }
  }
}

const updateClarificationText = (item, value) => {
  clarificationAnswers.value = {
    ...clarificationAnswers.value,
    [item.item_id]: {
      option_id: '',
      answer_text: String(value || '')
    }
  }
}
const turnVersionDetailLoading = ref(false)
const previewArtifact = ref(null)
const evidenceDrawerVisible = ref(false)
const evidenceLoading = ref(false)
const evidenceDetail = ref(null)
const favoritesPanelVisible = ref(false)
const insightTasks = ref([])
const insightTasksLoading = ref(false)
const insightTaskPanelVisible = ref(false)
const insightTaskPanelOpened = ref(false)
const insightTaskDetailLoading = ref(false)
const selectedInsightTask = ref(null)
const selectedInsightTaskRun = ref(null)
const insightTaskPolling = ref(false)
const insightTaskRunsLoading = ref(false)
const insightTaskRunSubmitting = ref(false)
let insightTaskPollTimer = null
let insightTaskDetailRequestId = 0
let insightTaskRunRequestId = 0
const insightTaskEditorVisible = ref(false)
const insightTaskEditorLoading = ref(false)
const insightTaskEditorMode = ref('create')
const insightTaskEditorSource = ref(null)
const insightTaskEditorTask = ref(null)
const insightTaskEditorForm = ref({
  name: '',
  description: '',
  frequency: 'daily',
  time: '09:00',
  weekdays: [1],
  dayOfMonth: 1
})
const llmModels = ref([])
const llmModelsLoading = ref(false)
const selectedLlmModelId = ref('')

const weekdayOptions = [
  { label: '周一', value: 1 },
  { label: '周二', value: 2 },
  { label: '周三', value: 3 },
  { label: '周四', value: 4 },
  { label: '周五', value: 5 },
  { label: '周六', value: 6 },
  { label: '周日', value: 7 }
]

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

const selectedLlmModel = computed(() =>
  llmModels.value.find((item) => item.id === selectedLlmModelId.value) || null
)

const renderMarkdown = (content) => (content ? marked.parse(content) : '')
const escapeHtml = (value) => String(value ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;')

const renderTraceableReport = (content, refs = []) => {
  const text = String(content || '')
  const characters = Array.from(text)
  const normalizedRefs = (Array.isArray(refs) ? refs : [])
    .filter((ref) => ref && Number.isFinite(Number(ref.start)) && Number.isFinite(Number(ref.end)) && ref.evidence_id)
    .map((ref) => ({ ...ref, start: Number(ref.start), end: Number(ref.end) }))
    .filter((ref) => ref.start >= 0 && ref.end > ref.start && ref.end <= characters.length)
    .sort((left, right) => left.start - right.start)

  if (!normalizedRefs.length) return renderMarkdown(text)

  let cursor = 0
  let markdown = ''
  normalizedRefs.forEach((ref) => {
    if (ref.start < cursor) return
    markdown += characters.slice(cursor, ref.start).join('')
    const targetText = characters.slice(ref.start, ref.end).join('')
    markdown += `<span class="traceable-value" data-evidence-id="${escapeHtml(ref.evidence_id)}" data-start="${ref.start}" data-end="${ref.end}">${escapeHtml(targetText)}</span>`
    cursor = ref.end
  })
  markdown += characters.slice(cursor).join('')
  return marked.parse(markdown)
}
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

const collectNumber = (value) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

const favoriteCharts = (collect) => {
  const metadata = parseCollectMetadata(collect)
  if (metadata.chart_spec && typeof metadata.chart_spec === 'object') {
    return [{
      artifactId: collectNumber(metadata.artifact_id || metadata.artifactId || 0),
      chartSpec: sanitizeChartSpec(metadata.chart_spec)
    }].filter((item) => item.chartSpec)
  }
  if (Array.isArray(metadata.charts) && metadata.charts.length > 0) {
    return metadata.charts
      .map((chart) => ({
        artifactId: collectNumber(chart?.artifactId || chart?.artifact_id || 0),
        chartSpec: sanitizeChartSpec(chart?.chartSpec || chart?.chart_spec || null)
      }))
      .filter((item) => item.chartSpec)
  }
  return []
}

const favoriteChartSpec = (collect) => {
  return favoriteCharts(collect)[0]?.chartSpec || null
}

const formatChartNumber = (value) => {
  if (value === null || value === undefined || value === '') return ''
  const numericValue = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numericValue)) return value
  if (Number.isInteger(numericValue)) {
    return numericValue.toLocaleString('zh-CN')
  }
  return numericValue.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
}

const applyTitleLegendGridRules = (normalized) => {
  const title = Array.isArray(normalized.title) ? normalized.title[0] : normalized.title
  const legend = Array.isArray(normalized.legend) ? normalized.legend[0] : normalized.legend

  const titleItem = title && typeof title === 'object' ? title : {}
  const legendItem = legend && typeof legend === 'object' ? legend : {}

  const titlePresent = Boolean(titleItem.text)
  const legendPresent = Boolean(Object.keys(legendItem).length)

  if (titlePresent) {
    titleItem.top = titleItem.top ?? 8
    titleItem.left = titleItem.left ?? 'center'
    titleItem.textStyle = titleItem.textStyle && typeof titleItem.textStyle === 'object' ? titleItem.textStyle : {}
    titleItem.textStyle.fontSize = titleItem.textStyle.fontSize || 16
    titleItem.textStyle.fontWeight = titleItem.textStyle.fontWeight || 600
  }

  if (legendPresent) {
    legendItem.type = legendItem.type || 'scroll'
    legendItem.left = legendItem.left ?? 'center'
    legendItem.top = legendItem.top ?? (titlePresent ? 36 : 8)
    legendItem.itemWidth = legendItem.itemWidth || 14
    legendItem.itemHeight = legendItem.itemHeight || 10
    legendItem.textStyle = legendItem.textStyle && typeof legendItem.textStyle === 'object' ? legendItem.textStyle : {}
    legendItem.textStyle.fontSize = legendItem.textStyle.fontSize || 11
  }

  if (Array.isArray(normalized.title)) {
    normalized.title[0] = titleItem
  } else if (titlePresent) {
    normalized.title = titleItem
  }

  if (Array.isArray(normalized.legend)) {
    normalized.legend[0] = legendItem
  } else if (legendPresent) {
    normalized.legend = legendItem
  }

  const currentGrid = Array.isArray(normalized.grid) ? normalized.grid[0] : normalized.grid
  const gridItem = currentGrid && typeof currentGrid === 'object' ? currentGrid : {}

  let topOffset = 56
  if (titlePresent && legendPresent) topOffset = 96
  else if (titlePresent) topOffset = 64
  else if (legendPresent) topOffset = 72

  gridItem.top = gridItem.top ?? topOffset
  gridItem.left = gridItem.left ?? 56
  gridItem.right = gridItem.right ?? 24
  gridItem.bottom = gridItem.bottom ?? 56
  gridItem.containLabel = gridItem.containLabel ?? true

  if (Array.isArray(normalized.grid)) {
    normalized.grid[0] = gridItem
  } else {
    normalized.grid = gridItem
  }
}

const applyAxisDisplayRules = (normalized) => {
  ;['xAxis', 'yAxis'].forEach((axisKey) => {
    const axes = normalized[axisKey]
    const axisList = Array.isArray(axes) ? axes : (axes && typeof axes === 'object' ? [axes] : [])
    axisList.forEach((axis) => {
      if (!axis || typeof axis !== 'object') return
      axis.axisLabel = axis.axisLabel && typeof axis.axisLabel === 'object' ? axis.axisLabel : {}
      axis.axisLabel.hideOverlap = true
      axis.axisLabel.fontSize = axis.axisLabel.fontSize || 11
      axis.axisLabel.margin = axis.axisLabel.margin || 10

      if (axis.type === 'value' || axis.type === 'log') {
        axis.axisLabel.formatter = (value) => formatChartNumber(value)
      }

      const categoryData = Array.isArray(axis.data) ? axis.data : []
      const maxLabelLength = categoryData.reduce((max, item) => Math.max(max, String(item ?? '').length), 0)
      if (axis.type === 'category' && (categoryData.length > 8 || maxLabelLength > 8)) {
        axis.axisLabel.rotate = axis.axisLabel.rotate || (maxLabelLength > 14 ? 45 : 30)
      }
    })
  })
}

const applySeriesDisplayRules = (normalized) => {
  if (!Array.isArray(normalized.series)) return

  normalized.series.forEach((series) => {
    if (!series || typeof series !== 'object') return

    series.label = series.label && typeof series.label === 'object' ? series.label : {}
    series.label.fontSize = series.label.fontSize || 11
    series.label.overflow = series.label.overflow || 'truncate'

    if (series.label.show) {
      const originalFormatter = series.label.formatter
      series.label.formatter = (params) => {
        if (typeof originalFormatter === 'function') {
          return originalFormatter(params)
        }
        const rawValue = Array.isArray(params?.value) ? params.value[params.value.length - 1] : params?.value
        return formatChartNumber(rawValue)
      }
    }

    series.labelLayout = series.labelLayout && typeof series.labelLayout === 'object' ? series.labelLayout : {}
    series.labelLayout.hideOverlap = true

    if (series.type === 'bar') {
      series.barMinHeight = Math.max(series.barMinHeight || 0, 6)
    }

    if (series.type === 'pie') {
      series.avoidLabelOverlap = true
      series.minAngle = Math.max(series.minAngle || 0, 3)
      series.percentPrecision = series.percentPrecision || 2
      if (series.label.show !== false) {
        const originalFormatter = series.label.formatter
        series.label.formatter = (params) => {
          if (typeof originalFormatter === 'function') {
            return originalFormatter(params)
          }
          return `${params?.name || ''}\n${formatChartNumber(params?.value)} (${(params?.percent ?? 0).toFixed(2)}%)`
        }
      }
    }
  })
}

const applyTooltipDisplayRules = (normalized) => {
  normalized.tooltip = normalized.tooltip && typeof normalized.tooltip === 'object' ? normalized.tooltip : {}
  const originalFormatter = normalized.tooltip.formatter
  if (typeof originalFormatter !== 'function') {
    normalized.tooltip.formatter = (params) => {
      const rows = Array.isArray(params) ? params : [params]
      return rows
        .filter(Boolean)
        .map((item) => {
          const marker = item.marker || ''
          const seriesName = item.seriesName || item.name || ''
          const rawValue = Array.isArray(item.value) ? item.value[item.value.length - 1] : item.value
          return `${marker}${seriesName}: ${formatChartNumber(rawValue)}`
        })
        .join('<br/>')
    }
  }
}

const sanitizeChartSpec = (chartSpec) => {
  if (!chartSpec || typeof chartSpec !== 'object') return null
  const normalized = JSON.parse(JSON.stringify(chartSpec))
  const backendManaged = normalized.__layout_managed_by_backend === true
  delete normalized.__layout_managed_by_backend

  normalized.animation = false
  normalized.animationDuration = 0
  normalized.animationDurationUpdate = 0
  normalized.animationDelay = 0
  normalized.animationDelayUpdate = 0
  applyPieLegendSafety(normalized)

  if (backendManaged) {
    applyTooltipDisplayRules(normalized)
    return normalized
  }

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

  applyTitleLegendGridRules(normalized)
  applyAxisDisplayRules(normalized)
  applySeriesDisplayRules(normalized)
  applyTooltipDisplayRules(normalized)

  return normalized
}

const normalizeChartItem = (chart) => {
  if (!chart || typeof chart !== 'object') return null
  return {
    id: chart.id || 0,
    title: chart.title || '',
    chartType: chart.chart_type || '',
    chartSpec: sanitizeChartSpec(chart.chart_spec || null),
    evidenceRefs: chart.evidence_refs || chart.metadata_json?.evidence_refs || {},
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
const getDisplayTables = (item) => Array.isArray(item?.tables) ? item.tables : []
const getPrimaryChart = (item) => getDisplayCharts(item)[0] || null
const artifactChartSpec = (artifact) => sanitizeChartSpec(artifact?.content_json?.chart_spec || null)
const getArtifactEvidenceRefs = (artifact) => artifact?.metadata_json?.evidence_refs || artifact?.evidence_refs || {}

const getTableColumns = (table) => {
  if (Array.isArray(table?.columns) && table.columns.length > 0) {
    return table.columns
      .map((column) => {
        if (typeof column === 'string') return { field: column, title: column }
        const field = column?.field || column?.key || column?.name || column?.title
        if (!field) return null
        return {
          ...column,
          field,
          title: column?.title || column?.label || column?.name || field
        }
      })
      .filter(Boolean)
  }
  const firstRow = Array.isArray(table?.rows)
    ? table.rows.find((row) => row && typeof row === 'object' && !Array.isArray(row))
    : null
  return firstRow
    ? Object.keys(firstRow).map((field) => ({ field, title: field }))
    : []
}

const getTableRows = (table) => Array.isArray(table?.rows) ? table.rows : []
const getTableEvidenceRefs = (table) => table?.evidence_refs?.table_trace_refs || table?.metadata_json?.evidence_refs?.table_trace_refs || []
const normalizeReportRefs = (value) => {
  if (Array.isArray(value)) return value
  return value?.report_trace_refs || []
}
const getReportEvidenceRefs = (item) => normalizeReportRefs(item?.reportEvidenceRefs || item?.report_evidence_refs)
const currentReportEvidenceRefs = computed(() => normalizeReportRefs(currentReportRefs.value))

const getVersionReportEvidenceRefs = (detail) => {
  const reportArtifact = getVersionArtifactsByType(detail, 'report')[0]
  return getArtifactEvidenceRefs(reportArtifact)?.report_trace_refs || []
}

const getTurnDetailReportEvidenceRefs = (detail) => {
  const reportArtifact = Array.isArray(detail?.artifacts)
    ? detail.artifacts.find((artifact) => artifact?.artifact_type === 'report')
    : null
  return getArtifactEvidenceRefs(reportArtifact)?.report_trace_refs || []
}

const normalizeTables = (tables = []) => (
  (Array.isArray(tables) ? tables : []).map((table) => ({
    ...table,
    evidence_refs: table.evidence_refs || table.metadata_json?.evidence_refs || {}
  }))
)

const formatTableCell = (value) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return formatChartNumber(value)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const getRefValueField = (ref) => String(ref?.value_field || ref?.field || ref?.x_field || ref?.y_field || '').trim()
const getRefTableColumn = (ref) => String(ref?.column_key || ref?.value_column || ref?.column || '').trim()
const tableRefMatchesRow = (ref, row) => {
  const selector = ref?.row_selector
  if (!selector || typeof selector !== 'object' || Array.isArray(selector)) return true
  if (!row || typeof row !== 'object' || Array.isArray(row)) return false
  return Object.entries(selector).every(([field, value]) => String(row[field]) === String(value))
}

const hasTableEvidenceRef = (table, row, column) => (
  getTableEvidenceRefs(table).some((ref) => (
    (getRefTableColumn(ref) === column.field || getRefTableColumn(ref) === column.title)
    && tableRefMatchesRow(ref, row)
  ))
)

const isPlainObject = (value) => value && typeof value === 'object' && !Array.isArray(value)

const chartDimensionName = (dimensions, value) => {
  const index = Number(value)
  if (!Number.isFinite(index) || index < 0 || index >= dimensions.length) {
    return typeof value === 'string' ? value : ''
  }
  const dimension = dimensions[index]
  if (typeof dimension === 'string' || typeof dimension === 'number') return String(dimension)
  if (isPlainObject(dimension) && (dimension.name !== undefined || dimension.displayName !== undefined)) {
    return String(dimension.name ?? dimension.displayName)
  }
  return typeof value === 'string' ? value : ''
}

const normalizeEncodedFields = (value, dimensions) => (
  (Array.isArray(value) ? value : [])
    .map((item) => (
      typeof item === 'string' && Number.isNaN(Number(item))
        ? item.trim()
        : chartDimensionName(dimensions, item).trim()
    ))
    .filter(Boolean)
)

const chartDatasetDimensions = (chartSpec) => {
  const dataset = chartSpec?.dataset
  if (Array.isArray(dataset)) {
    return isPlainObject(dataset[0]) && Array.isArray(dataset[0].dimensions) ? dataset[0].dimensions : []
  }
  return isPlainObject(dataset) && Array.isArray(dataset.dimensions) ? dataset.dimensions : []
}

const seriesEncodedFields = (chartSpec, seriesIndex) => {
  const series = Array.isArray(chartSpec?.series) ? chartSpec.series[seriesIndex] : null
  if (!isPlainObject(series) || !isPlainObject(series.encode)) return []
  const dimensions = chartDatasetDimensions(chartSpec)
  const fields = []
  ;['x', 'y', 'value', 'tooltip'].forEach((key) => {
    const encoded = series.encode[key]
    if (typeof encoded === 'string') fields.push(encoded.trim())
    else if (typeof encoded === 'number') {
      const field = chartDimensionName(dimensions, encoded)
      if (field) fields.push(field)
    } else if (Array.isArray(encoded)) {
      fields.push(...normalizeEncodedFields(encoded, dimensions))
    }
  })
  return fields
}

const chartEventValueFields = (chartSpec, params = {}) => {
  const dimensions = chartDatasetDimensions(chartSpec)
  const encode = params.encode || {}
  const eventFields = [
    ...normalizeEncodedFields(encode.y, dimensions),
    ...normalizeEncodedFields(encode.x, dimensions),
    ...seriesEncodedFields(chartSpec, Number(params.seriesIndex ?? 0)),
    String(params.seriesName ?? '').trim(),
    String(params.name ?? '').trim()
  ].filter(Boolean)
  return Array.from(new Set(eventFields))
}

const chartRefsForInteraction = (refs, chartIndex, artifactId) => {
  if (!refs.length) return []
  const hasArtifactId = artifactId !== null && artifactId !== undefined && artifactId !== ''
  if (
    hasArtifactId
    && refs.every((ref) => ref.artifact_id !== null && ref.artifact_id !== undefined && String(ref.artifact_id) === String(artifactId))
  ) {
    return refs
  }
  const indexed = refs.filter((ref) => Number(ref.chart_index ?? 0) === Number(chartIndex ?? 0))
  if (indexed.length) return indexed
  return hasArtifactId
    ? refs.filter((ref) => ref.artifact_id !== null && ref.artifact_id !== undefined && String(ref.artifact_id) === String(artifactId))
    : []
}

const normalizeEvidenceField = (value) => String(value || '')
  .replace(/\s+/g, '')
  .replace(/[()（）]/g, '')
  .replace(/_/g, '')
  .toLowerCase()

const chartRefMatchesField = (ref, valueFields, seriesName) => {
  const refField = getRefValueField(ref)
  if (!refField || valueFields.includes(refField)) return true
  const normalizedRef = normalizeEvidenceField(refField)
  if (valueFields.some((field) => normalizeEvidenceField(field) === normalizedRef)) return true
  const normalizedSeries = normalizeEvidenceField(seriesName)
  return Boolean(
    normalizedSeries
    && (
      normalizedSeries === normalizedRef
      || normalizedSeries.includes(normalizedRef)
      || normalizedRef.includes(normalizedSeries)
    )
  )
}

const chartRefMatchesPoint = (ref, params = {}) => {
  const selector = ref?.point_selector
  if (!selector || typeof selector !== 'object' || Array.isArray(selector)) return true
  const point = isPlainObject(params?.data) ? params.data : {}
  const pointName = String(params?.name ?? '').trim()
  return Object.entries(selector).every(([field, expected]) => {
    if (field in point) return String(point[field]) === String(expected)
    return pointName !== '' && String(expected) === pointName
  })
}

const findChartEvidenceRef = (chart, chartIndex, params = {}) => {
  const refs = chart?.evidenceRefs?.chart_trace_refs || []
  const candidates = chartRefsForInteraction(refs, chartIndex, chart?.id)
  if (!candidates.length) return null
  const pointCandidates = candidates.filter((ref) => chartRefMatchesPoint(ref, params))
  const scopedCandidates = pointCandidates.length ? pointCandidates : candidates.filter((ref) => !ref?.point_selector)
  if (!scopedCandidates.length) return null
  const valueFields = chartEventValueFields(chart?.chartSpec, params)
  const semanticMatch = scopedCandidates.find((ref) => chartRefMatchesField(ref, valueFields, params.seriesName))
  if (semanticMatch) return semanticMatch
  if (scopedCandidates.length === 1) return scopedCandidates[0]
  const seriesIndex = Number(params?.seriesIndex ?? -1)
  return seriesIndex >= 0 && seriesIndex < scopedCandidates.length ? scopedCandidates[seriesIndex] : null
}

const structuredChartValue = (value, field) => {
  if (field && isPlainObject(value) && field in value) return value[field]
  if (Array.isArray(value)) return field ? undefined : value.length > 1 ? value[value.length - 1] : value[0]
  return undefined
}

const chartEventRawValue = (chartSpec, params, valueField) => {
  const dataValue = structuredChartValue(params?.data, valueField)
  if (dataValue !== undefined) return dataValue
  if (Array.isArray(params?.value)) return params.value.length > 1 ? params.value[params.value.length - 1] : params.value[0]
  if (params?.value !== null && params?.value !== undefined) return params.value
  const dataIndex = Number(params?.dataIndex ?? -1)
  if (!valueField || dataIndex < 0) return undefined
  const dataset = chartSpec?.dataset
  const source = isPlainObject(dataset) && Array.isArray(dataset.source)
    ? dataset.source
    : Array.isArray(dataset) && isPlainObject(dataset[0]) && Array.isArray(dataset[0].source)
      ? dataset[0].source
      : null
  return source && dataIndex < source.length ? structuredChartValue(source[dataIndex], valueField) : undefined
}

const findTableEvidenceRef = (table, row, column) => {
  const refs = getTableEvidenceRefs(table)
  const columnRefs = refs.filter((ref) => (
    getRefTableColumn(ref) === column.field || getRefTableColumn(ref) === column.title
  ))
  return columnRefs.find((ref) => ref?.row_selector && tableRefMatchesRow(ref, row))
    || columnRefs.find((ref) => !ref?.row_selector && String(ref?.row_scope || 'all') === 'all')
    || columnRefs.find((ref) => tableRefMatchesRow(ref, row))
}

const resolveEvidenceDetail = async ({ turnId, artifactId, evidenceId, selection }) => {
  if (!activeConversationId.value || !turnId || !artifactId || !evidenceId) return
  evidenceLoading.value = true
  evidenceDetail.value = null
  evidenceDrawerVisible.value = true
  try {
    const response = await resolveEvidence(activeConversationId.value, turnId, {
      artifact_id: artifactId,
      evidence_id: evidenceId,
      selection: selection || {},
      detail_level: 'full'
    })
    evidenceDetail.value = response.data?.data || null
  } catch (error) {
    console.error('Resolve evidence error:', error)
    ElMessage.error(error?.response?.data?.message || '证据链加载失败')
    evidenceDrawerVisible.value = false
  } finally {
    evidenceLoading.value = false
  }
}

const onChartEvidenceClick = (item, chart, chartIndex, params) => {
  const ref = findChartEvidenceRef(chart, chartIndex, params)
  if (!ref?.evidence_id) return
  resolveEvidenceDetail({
    turnId: item?.turnId,
    artifactId: ref.artifact_id || chart.id,
    evidenceId: ref.evidence_id,
    selection: {
      type: 'chart',
      chart_index: chartIndex,
      data_index: Number(params?.dataIndex ?? -1),
      series_index: Number(params?.seriesIndex ?? -1),
      value_field: getRefValueField(ref),
      point_selector: ref?.point_selector || undefined,
      raw_value: chartEventRawValue(chart?.chartSpec, params, getRefValueField(ref))
    }
  })
}

const onArtifactChartEvidenceClick = (artifact, params) => {
  const refs = getArtifactEvidenceRefs(artifact)
  const chart = {
    id: artifact?.id,
    evidenceRefs: refs,
    chartSpec: artifactChartSpec(artifact)
  }
  onChartEvidenceClick({ turnId: artifact?.turn_id || turnDetail.value?.turn?.id }, chart, 0, params)
}

const onTableEvidenceClick = (item, table, tableIndex, row, rowIndex, column) => {
  const ref = findTableEvidenceRef(table, row, column)
  if (!ref?.evidence_id) return
  resolveEvidenceDetail({
    turnId: item?.turnId,
    artifactId: ref.artifact_id || table.id,
    evidenceId: ref.evidence_id,
    selection: {
      type: 'table',
      table_index: tableIndex,
      row_index: rowIndex,
      column_key: column.field,
      raw_value: row?.[column.field]
    }
  })
}

const onReportEvidenceClick = (item, event) => {
  const target = event.target?.closest?.('.traceable-value')
  if (!target) return
  const evidenceId = target.dataset.evidenceId
  const start = Number(target.dataset.start)
  const end = Number(target.dataset.end)
  const refs = normalizeReportRefs(item?.reportEvidenceRefs || item?.report_evidence_refs)
  const ref = refs.find((candidate) => candidate.evidence_id === evidenceId && Number(candidate.start) === start && Number(candidate.end) === end)
  if (!ref?.evidence_id) return
  resolveEvidenceDetail({
    turnId: item?.turnId,
    artifactId: ref.artifact_id,
    evidenceId: ref.evidence_id,
    selection: {
      type: 'report',
      start,
      end,
      raw_value: target.textContent || ''
    }
  })
}

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
    container.style.height = `${Math.max(420, Math.min(620, (chart.chartSpec?.grid?.top || 0) + 420))}px`
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
  id: event.progress?.activity_id || event.event_id || `${Date.now()}-${Math.random().toString(16).slice(2)}`,
  eventId: event.event_id || '',
  title: event.progress?.title || '',
  state: event.progress?.state || '',
  level: event.level || (event.type === 'assistant' ? 'assistant' : 'info'),
  message: event.message
})

const normalizeProgressEvents = (events = []) => {
  const items = []
  const positions = new Map()
  for (const event of events) {
    if (!event?.message) continue
    const item = createProgressItem(event)
    if (positions.has(item.id)) {
      items[positions.get(item.id)] = item
    } else {
      positions.set(item.id, items.length)
      items.push(item)
    }
  }
  return items
}

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
  const item = createProgressItem(event)
  const existingIndex = currentProgressItems.value.findIndex((value) => value.id === item.id)
  if (existingIndex >= 0) {
    currentProgressItems.value.splice(existingIndex, 1, item)
    scrollToBottom()
    return
  }
  const lastItem = currentProgressItems.value[currentProgressItems.value.length - 1]
  if (lastItem?.message === event.message) return
  currentProgressItems.value.push(item)
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
  currentReportRefs.value = {}
  currentAssistantMessage.value = ''
  currentProgressItems.value = []
  currentTaskId.value = ''
  currentTurnId.value = 0
  currentRunMode.value = 'new'
  currentClarification.value = null
  clarificationAnswers.value = {}
  clarificationSubmitting.value = false
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
    reportEvidenceRefs: currentReportRefs.value,
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

const removeHistoryTurn = (turnId) => {
  const normalizedTurnId = Number(turnId || 0)
  if (!normalizedTurnId) return
  chatHistory.value = chatHistory.value.filter((item) => Number(item.turnId) !== normalizedTurnId)
}

const subscribeTurnStream = (conversationId, turnId) => {
  currentStreamController.value = streamTurnEvents(
    conversationId,
    turnId,
    handleStreamEvent,
    handleStreamError,
    handleStreamDone
  )
}

const attachRunningTurn = (task) => {
  if (!task?.conversation_id || !task?.turn_id) return false
  activeConversationId.value = Number(task.conversation_id)
  currentTaskId.value = task.task_id || ''
  currentTurnId.value = Number(task.turn_id)
  currentRunMode.value = task.task_type === 'rerun' ? 'rerun' : 'new'
  currentQuestion.value = task.request?.user_message || currentQuestion.value || '正在分析中...'
  const runningHistory = chatHistory.value.find((item) => Number(item.turnId) === Number(task.turn_id))
  currentProgressItems.value = [...(runningHistory?.progressItems || [])]
  removeHistoryTurn(task.turn_id)
  loading.value = true
  const resumeMessage = task.status === 'queued'
    ? '检测到当前会话有排队中的分析任务，正在等待执行...'
    : task.status === 'waiting_clarification'
      ? '检测到当前分析正在等待补充口径...'
      : '检测到当前会话有正在进行的分析，正在重新订阅实时输出...'
  addProgressItem({
    type: 'status',
    level: 'info',
    message: resumeMessage
  })
  subscribeTurnStream(task.conversation_id, task.turn_id)
  return true
}

const resumeRunningTurnIfAny = async (conversationId) => {
  if (!conversationId) return false
  const response = await getRunningTurn(conversationId)
  const runningTurn = response.data?.data
  if (!runningTurn) {
    loading.value = false
    return false
  }
  return attachRunningTurn(runningTurn)
}

const mapHistoryItem = (item) => ({
  turnId: item.turn_id,
  question: item.question,
  latestExecution: item.latest_execution || null,
  chartArtifactId: item.chart_artifact_id || 0,
  charts: normalizeCharts(item.charts || []),
  tables: normalizeTables(item.tables || []),
  report: item.report,
  reportEvidenceRefs: item.report_evidence_refs || {},
  progressItems: normalizeProgressEvents(item.progress || [])
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
    const response = await listCollects()
    if (response.data.success) collects.value = response.data.data || []
  } catch (error) {
    console.error('List collects error:', error)
  }
}

const fetchInsightTasks = async ({ silent = false } = {}) => {
  if (!silent) insightTasksLoading.value = true
  try {
    const response = await listInsightTasks()
    if (response.data?.success) insightTasks.value = response.data.data || []
  } catch (error) {
    console.error('List insight tasks error:', error)
  } finally {
    if (!silent) insightTasksLoading.value = false
  }
}

const isPendingTaskRun = (run) => ['queued', 'running'].includes(String(run?.status || ''))

const hasPendingTaskRun = (task) => (
  isPendingTaskRun(task?.latest_run)
  || (Array.isArray(task?.runs) && task.runs.some(isPendingTaskRun))
  || (task?.task_id === selectedInsightTask.value?.task_id && isPendingTaskRun(selectedInsightTaskRun.value))
)

const hasAnyPendingTaskRun = () => (
  insightTasks.value.some(hasPendingTaskRun)
  || hasPendingTaskRun(selectedInsightTask.value)
)

const formatTaskRunStatus = (run) => {
  const labels = {
    queued: '等待执行',
    running: '正在执行',
    success: '最近执行成功',
    failed: '最近执行失败',
    cancelled: '最近执行已取消'
  }
  return labels[String(run?.status || '')] || '尚未执行'
}

const hasRunnableExecution = (execution) => Boolean(
  execution?.id
  && (!execution.execution_status || execution.execution_status === 'success')
  && (execution.is_runnable === true || String(execution.generated_code || '').trim())
)

const hasTaskRunnableExecution = (execution) => hasRunnableExecution(execution)

const getTaskDisabledReason = (execution) => {
  if (!execution?.id) return '当前轮次没有可复用的执行记录'
  if (execution.execution_status && execution.execution_status !== 'success') return '只有成功的分析结果可以加入任务'
  if (!hasRunnableExecution(execution)) return '当前执行记录缺少可复用分析代码'
  return '将当前分析逻辑保存为洞察任务'
}

const buildInsightTaskEditorForm = ({ name = '', description = '', schedule = {} } = {}) => {
  const frequency = ['daily', 'weekdays', 'weekly', 'monthly'].includes(schedule?.frequency)
    ? schedule.frequency
    : 'daily'
  const weekdays = Array.isArray(schedule?.weekdays)
    ? schedule.weekdays.map(Number).filter((item) => item >= 1 && item <= 7)
    : []
  return {
    name: String(name || '').slice(0, 260),
    description: String(description || ''),
    frequency,
    time: schedule?.time || '09:00',
    weekdays: weekdays.length ? weekdays : [1],
    dayOfMonth: Math.min(31, Math.max(1, Number(schedule?.day_of_month) || 1))
  }
}

const buildInsightTaskSchedule = (form) => {
  const schedule = {
    frequency: form.frequency,
    time: form.time || '09:00'
  }
  if (form.frequency === 'weekly') schedule.weekdays = [...form.weekdays]
  if (form.frequency === 'monthly') schedule.day_of_month = form.dayOfMonth || 1
  return schedule
}

const validateInsightTaskEditor = (form) => {
  if (!String(form.name || '').trim()) {
    ElMessage.warning('请输入任务名称')
    return false
  }
  if (form.frequency === 'weekly' && !form.weekdays.length) {
    ElMessage.warning('请至少选择一个执行星期')
    return false
  }
  return true
}

const openCreateTask = (item) => {
  const execution = item?.latestExecution
  if (!hasTaskRunnableExecution(execution)) {
    ElMessage.warning(getTaskDisabledReason(execution))
    return
  }
  insightTaskEditorMode.value = 'create'
  insightTaskEditorSource.value = execution
  insightTaskEditorTask.value = null
  insightTaskEditorForm.value = buildInsightTaskEditorForm({
    name: String(item?.question || execution.title || '洞察任务').slice(0, 260),
    description: execution.description || '',
    schedule: { frequency: 'daily', time: '09:00' }
  })
  insightTaskEditorVisible.value = true
}

const openEditInsightTask = (task) => {
  if (!task?.task_id) return
  insightTaskEditorMode.value = 'edit'
  insightTaskEditorSource.value = null
  insightTaskEditorTask.value = task
  insightTaskEditorForm.value = buildInsightTaskEditorForm(task)
  insightTaskEditorVisible.value = true
}

const submitInsightTaskEditor = async () => {
  const form = insightTaskEditorForm.value
  if (!validateInsightTaskEditor(form)) return
  insightTaskEditorLoading.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description.trim(),
      schedule: buildInsightTaskSchedule(form)
    }
    if (insightTaskEditorMode.value === 'edit') {
      const task = insightTaskEditorTask.value
      if (!task?.task_id) return
      const response = await updateInsightTask(task.task_id, payload)
      if (!response.data?.success) throw new Error(response.data?.message || '更新任务失败')
      selectedInsightTask.value = response.data.data
      insightTaskEditorVisible.value = false
      ElMessage.success('洞察任务已更新')
      await fetchInsightTasks({ silent: true })
      scheduleInsightTaskPolling()
      return
    }

    const execution = insightTaskEditorSource.value
    if (!hasTaskRunnableExecution(execution)) throw new Error('当前执行记录不可复用')
    const response = await createInsightTask({
      source_execution_id: execution.id,
      ...payload
    })
    if (!response.data?.success) throw new Error(response.data?.message || '创建任务失败')
    insightTaskEditorVisible.value = false
    ElMessage.success('洞察任务已创建')
    await fetchInsightTasks()
  } catch (error) {
    const fallback = insightTaskEditorMode.value === 'edit' ? '更新任务失败' : '创建任务失败'
    ElMessage.error(error?.response?.data?.message || error?.message || fallback)
  } finally {
    insightTaskEditorLoading.value = false
  }
}

const openInsightTasks = async () => {
  insightTaskPanelVisible.value = true
  await fetchInsightTasks()
  if (!selectedInsightTask.value && insightTasks.value.length) {
    await selectInsightTask(insightTasks.value[0])
  }
  scheduleInsightTaskPolling()
}

const selectInsightTask = async (task) => {
  if (!task?.task_id) return
  const taskId = task.task_id
  const requestId = ++insightTaskDetailRequestId
  insightTaskRunRequestId += 1
  insightTaskDetailLoading.value = true
  selectedInsightTaskRun.value = null
  try {
    const response = await getInsightTask(taskId)
    if (requestId !== insightTaskDetailRequestId) return
    if (!response.data?.success) throw new Error(response.data?.message || '加载任务失败')
    selectedInsightTask.value = response.data.data
    const successfulRun = (selectedInsightTask.value.runs || []).find((item) => item.status === 'success')
    if (successfulRun) await selectInsightTaskRun(successfulRun)
    scheduleInsightTaskPolling()
  } catch (error) {
    if (requestId !== insightTaskDetailRequestId) return
    ElMessage.error(error?.response?.data?.message || error?.message || '加载任务失败')
  } finally {
    if (requestId === insightTaskDetailRequestId) insightTaskDetailLoading.value = false
  }
}

const refreshSelectedInsightTask = async () => {
  const taskId = selectedInsightTask.value?.task_id
  if (!taskId) return
  const previousRunId = selectedInsightTaskRun.value?.run_id || ''
  const previousRunStatus = selectedInsightTaskRun.value?.status || ''
  const response = await getInsightTask(taskId)
  if (selectedInsightTask.value?.task_id !== taskId) return
  if (!response.data?.success) return
  const detail = response.data.data
  const loadedRuns = selectedInsightTask.value?.runs || []
  if (loadedRuns.length > (detail.runs || []).length) {
    const mergedRuns = new Map((detail.runs || []).map((item) => [item.run_id, item]))
    for (const item of loadedRuns) {
      if (!mergedRuns.has(item.run_id)) mergedRuns.set(item.run_id, item)
    }
    detail.runs = [...mergedRuns.values()].sort((left, right) => (
      String(right.scheduled_for || '').localeCompare(String(left.scheduled_for || ''))
    ))
  }
  selectedInsightTask.value = detail
  const runs = detail.runs || []
  const previousRun = runs.find((item) => item.run_id === previousRunId)
  const latestSuccess = runs.find((item) => item.status === 'success')
  const targetRun = previousRun || latestSuccess
  if (
    targetRun?.run_id
    && (
      targetRun.run_id !== previousRunId
      || targetRun.status !== previousRunStatus
      || !selectedInsightTaskRun.value?.result_payload
    )
  ) {
    await selectInsightTaskRun(targetRun)
  }
}

const pollInsightTasks = async () => {
  if (!insightTaskPanelVisible.value || insightTaskPolling.value) return
  insightTaskPolling.value = true
  try {
    await fetchInsightTasks({ silent: true })
    await refreshSelectedInsightTask()
  } catch (error) {
    console.error('Poll insight tasks error:', error)
  } finally {
    insightTaskPolling.value = false
    scheduleInsightTaskPolling()
  }
}

const scheduleInsightTaskPolling = () => {
  stopInsightTaskPolling()
  if (!insightTaskPanelVisible.value || !hasAnyPendingTaskRun()) return
  insightTaskPollTimer = window.setTimeout(() => {
    insightTaskPollTimer = null
    pollInsightTasks()
  }, 2000)
}

const stopInsightTaskPolling = () => {
  if (!insightTaskPollTimer) return
  window.clearTimeout(insightTaskPollTimer)
  insightTaskPollTimer = null
}

const selectInsightTaskRun = async (run) => {
  if (!selectedInsightTask.value?.task_id || !run?.run_id) return
  const taskId = selectedInsightTask.value.task_id
  const runId = run.run_id
  const requestId = ++insightTaskRunRequestId
  try {
    const response = await getInsightTaskRun(taskId, runId)
    if (
      requestId === insightTaskRunRequestId
      && selectedInsightTask.value?.task_id === taskId
      && response.data?.success
    ) {
      selectedInsightTaskRun.value = response.data.data
    }
  } catch (error) {
    if (requestId !== insightTaskRunRequestId) return
    ElMessage.error(error?.response?.data?.message || '加载执行记录失败')
  }
}

const loadMoreInsightTaskRuns = async () => {
  const task = selectedInsightTask.value
  if (!task?.task_id || insightTaskRunsLoading.value) return
  insightTaskRunsLoading.value = true
  try {
    const pageSize = 50
    const pageNo = Math.floor((task.runs || []).length / pageSize) + 1
    const response = await listInsightTaskRuns(task.task_id, pageNo, pageSize)
    if (!response.data?.success) throw new Error(response.data?.message || '加载执行记录失败')
    const page = response.data.data || {}
    const existing = task.runs || []
    const byId = new Map(existing.map((item) => [item.run_id, item]))
    for (const item of page.items || []) byId.set(item.run_id, item)
    task.runs = [...byId.values()].sort((left, right) => (
      String(right.scheduled_for || '').localeCompare(String(left.scheduled_for || ''))
    ))
    task.run_count = Number(page.total || task.run_count || task.runs.length)
  } catch (error) {
    ElMessage.error(error?.response?.data?.message || error?.message || '加载执行记录失败')
  } finally {
    insightTaskRunsLoading.value = false
  }
}

const getTaskRunCharts = (run) => (
  Array.isArray(run?.result_payload?.charts) ? run.result_payload.charts : []
)

const getTaskRunTables = (run) => (
  Array.isArray(run?.result_payload?.tables) ? run.result_payload.tables : []
)

const runSelectedInsightTask = async () => {
  const task = selectedInsightTask.value
  if (!task?.task_id || insightTaskRunSubmitting.value) return
  const snapshot = getTaskSnapshot(task)
  insightTaskRunSubmitting.value = true
  try {
    const response = await runInsightTask(task.task_id, snapshot.default_parameters || {})
    if (!response.data?.success) throw new Error(response.data?.message || '执行任务失败')
    const completedRun = response.data.data || null
    selectedInsightTaskRun.value = completedRun
    if (completedRun?.run_id) {
      const existingRuns = Array.isArray(selectedInsightTask.value?.runs) ? selectedInsightTask.value.runs : []
      selectedInsightTask.value.runs = [completedRun, ...existingRuns.filter((item) => item.run_id !== completedRun.run_id)]
      selectedInsightTask.value.latest_run = completedRun
    }
    await fetchInsightTasks({ silent: true })
    await refreshSelectedInsightTask()
    if (completedRun?.run_id) await selectInsightTaskRun(completedRun)
    if (completedRun?.status === 'success') {
      ElMessage.success('任务执行成功')
    } else {
      ElMessage.error(completedRun?.error_message || '任务执行失败')
    }
  } catch (error) {
    ElMessage.error(error?.response?.data?.message || error?.message || '执行任务失败')
  } finally {
    insightTaskRunSubmitting.value = false
  }
}

const toggleSelectedTaskStatus = async () => {
  const task = selectedInsightTask.value
  if (!task?.task_id) return
  const status = task.status === 'enabled' ? 'paused' : 'enabled'
  try {
    const response = await updateInsightTask(task.task_id, { status })
    if (!response.data?.success) throw new Error(response.data?.message || '更新任务失败')
    selectedInsightTask.value = response.data.data
    await fetchInsightTasks()
  } catch (error) {
    ElMessage.error(error?.response?.data?.message || error?.message || '更新任务失败')
  }
}

const removeSelectedInsightTask = async () => {
  const task = selectedInsightTask.value
  if (!task?.task_id) return
  try {
    await ElMessageBox.confirm(`确定删除洞察任务“${task.name}”吗？`, '删除洞察任务', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning'
    })
    await deleteInsightTask(task.task_id)
    selectedInsightTask.value = null
    selectedInsightTaskRun.value = null
    await fetchInsightTasks()
    if (insightTasks.value.length) await selectInsightTask(insightTasks.value[0])
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error?.response?.data?.message || '删除任务失败')
    }
  }
}

const getTaskSnapshot = (task) => task?.revision?.snapshot || {}

const getTaskDatasourceBindings = (task) => {
  const snapshot = getTaskSnapshot(task)
  if (Array.isArray(snapshot.datasource_bindings) && snapshot.datasource_bindings.length) {
    return snapshot.datasource_bindings.filter((item) => item && typeof item === 'object')
  }
  return (snapshot.used_datasource_ids || []).map((datasourceId) => ({ datasource_id: datasourceId }))
}

const getTaskQueryCriteria = (task, run) => {
  const snapshots = run?.result_payload?.query_snapshots
  if (snapshots && typeof snapshots === 'object' && !Array.isArray(snapshots)) {
    return Object.entries(snapshots)
      .filter(([, value]) => value && typeof value === 'object')
      .map(([queryKey, value]) => ({
        query_key: value.query_key || queryKey,
        query_constraints: value.query_constraints || {},
        query_scope: value.query_scope || {},
        datasource_ids: value.query_scope?.datasource_ids || [],
        execution: value.execution || {}
      }))
  }
  const sourceCriteria = getTaskSnapshot(task).query_criteria
  return Array.isArray(sourceCriteria) ? sourceCriteria : []
}

const uniqueCaliberValues = (values) => [...new Set(values.filter((item) => String(item || '').trim()).map((item) => String(item).trim()))]

const formatRelativeTimeRange = (value) => {
  if (!value || typeof value !== 'object' || value.kind !== 'relative_time_range') return ''
  const unitLabels = {
    minute: '分钟',
    hour: '小时',
    day: '天',
    week: '周',
    month: '个月',
    year: '年'
  }
  const amount = Number(value.amount)
  const unit = unitLabels[value.unit] || value.unit
  if (!Number.isFinite(amount) || amount <= 0 || !unit) return ''
  return `近 ${amount} ${unit}（随执行时间更新）`
}

const formatCaliberAtom = (value) => {
  if (value === null || value === undefined || value === '') return ''
  if (Array.isArray(value)) return value.map(formatCaliberAtom).filter(Boolean).join('、')
  if (typeof value === 'object') {
    const relativeTimeRange = formatRelativeTimeRange(value)
    if (relativeTimeRange) return relativeTimeRange
    return String(value.label || value.title || value.name || value.field || value.value || '').trim()
  }
  if (value === true) return '是'
  if (value === false) return '否'
  return String(value)
}

const formatTaskParameterEntries = (task, run) => {
  const snapshot = getTaskSnapshot(task)
  const schema = snapshot.parameter_schema || {}
  const values = run?.parameters || snapshot.default_parameters || schema.defaults || {}
  const parameters = Array.isArray(schema.parameters)
    ? schema.parameters.filter((parameter) => parameter && typeof parameter === 'object')
    : []
  return parameters.map((parameter) => {
    const name = parameter?.name
    const rawValue = values?.[name]
    const option = Array.isArray(parameter?.options)
      ? parameter.options.find((item) => (
          item && typeof item === 'object'
            ? String(item.value) === String(rawValue)
            : String(item) === String(rawValue)
        ))
      : null
    const displayValue = option && typeof option === 'object'
      ? formatCaliberAtom(option.label || option.title || option.value)
      : formatCaliberAtom(rawValue)
    if (!displayValue) return ''
    return `${parameter?.label || parameter?.description || name || '参数'}：${displayValue}`
  }).filter(Boolean)
}

const formatTaskTimeRange = (timeRange) => {
  if (!timeRange) return ''
  if (typeof timeRange === 'string') return timeRange
  if (typeof timeRange !== 'object') return formatCaliberAtom(timeRange)
  const relativeTimeRange = formatRelativeTimeRange(timeRange)
  if (relativeTimeRange) return relativeTimeRange
  const start = timeRange.start || timeRange.from || timeRange.start_time
  const end = timeRange.end || timeRange.to || timeRange.end_time
  if (!start && !end) return formatCaliberAtom(timeRange)
  return `${start || '不限'} 至 ${end || '不限'}`
}

const formatTaskFilter = (filter) => {
  if (!filter || typeof filter !== 'object') return formatCaliberAtom(filter)
  const field = filter.label || filter.semantic_role || filter.field || '条件'
  const value = filter.value ?? filter.values ?? filter.target ?? ''
  const renderedValue = formatCaliberAtom(value)
  return `${field} ${filter.operator || '='}${renderedValue ? ` ${renderedValue}` : ''}`
}

const getTaskStatisticalCaliber = (task, run) => {
  const criteria = getTaskQueryCriteria(task, run)
  const constraints = criteria
    .map((item) => item?.query_constraints)
    .filter((item) => item && typeof item === 'object' && Object.keys(item).length)
  const aggregations = constraints
    .map((item) => item.aggregation)
    .filter((item) => item && typeof item === 'object')
  const timeRanges = uniqueCaliberValues(constraints.map((item) => formatTaskTimeRange(item.time_range)))
  const dimensions = uniqueCaliberValues(aggregations.flatMap((item) => [
    ...[].concat(item.group_by || []),
    ...[].concat(item.dimensions || []),
    item.dimension,
    item.grain
  ].map(formatCaliberAtom)))
  const metrics = uniqueCaliberValues(aggregations.flatMap((item) => [
    ...[].concat(item.metrics || []),
    ...[].concat(item.measures || []),
    item.metric,
    item.measure
  ].map(formatCaliberAtom)))
  const filters = uniqueCaliberValues(constraints.flatMap((item) => (
    Array.isArray(item.target_filters) ? item.target_filters.map(formatTaskFilter) : []
  )))
  const dedupRules = uniqueCaliberValues(constraints.flatMap((item) => {
    const aggregation = item.aggregation && typeof item.aggregation === 'object' ? item.aggregation : {}
    return [
      item.dedup_rule,
      item.distinct_by,
      aggregation.dedup_rule,
      aggregation.distinct_by,
      aggregation.distinct_field,
      aggregation.distinct === true ? '按指标定义去重' : ''
    ].map(formatCaliberAtom)
  }))
  const definitions = uniqueCaliberValues(constraints.map((item) => item.notes || item.business_definition))
  const parameters = formatTaskParameterEntries(task, run)
  const bindings = getTaskDatasourceBindings(task)
    .map((item) => item.datasource_name || '')
    .filter(Boolean)

  return [
    { key: 'time', label: '时间范围', value: timeRanges.join('；') || '未限定' },
    { key: 'dimension', label: '维度', value: dimensions.join('、') || '按整体口径统计' },
    { key: 'metric', label: '指标', value: metrics.join('、') || '按固化分析逻辑计算' },
    { key: 'filter', label: '过滤', value: filters.join('；') || '无额外筛选' },
    { key: 'parameters', label: '参数取值', value: parameters.join('；') || '无可变参数' },
    { key: 'dedup', label: '去重规则', value: dedupRules.join('；') || '按固化查询逻辑执行' },
    { key: 'definition', label: '业务定义', value: definitions.join('；') || task?.description || '按分析目标执行', wide: true },
    { key: 'source', label: '口径来源', value: bindings.length ? bindings.join('、') : '来源分析任务', wide: true }
  ]
}

const formatTaskSchedule = (schedule = {}) => {
  const frequency = schedule?.frequency || 'manual'
  if (frequency === 'manual') return '仅手动执行'
  if (frequency === 'daily') return `每天 ${schedule.time || '09:00'}`
  if (frequency === 'weekdays') return `工作日 ${schedule.time || '09:00'}`
  if (frequency === 'monthly') return `每月 ${schedule.day_of_month || 1} 日 ${schedule.time || '09:00'}`
  const weekdayLabels = (schedule.weekdays || []).map((item) => weekdayOptions.find((option) => option.value === item)?.label || item)
  return `每周 ${weekdayLabels.join('、')} ${schedule.time || '09:00'}`
}

const formatTaskRunTrigger = (triggerType) => {
  if (triggerType === 'initial') return '初次执行'
  if (triggerType === 'manual') return '手动执行'
  return '定时执行'
}

const normalizeLlmModels = (items = []) =>
  (Array.isArray(items) ? items : [])
    .filter((item) => item && item.id)
    .map((item) => ({
      ...item,
      id: String(item.id),
      selected: Boolean(item.selected)
    }))

const getLlmModelLabel = (model) => {
  const owner = model?.owned_by ? ` · ${model.owned_by}` : ''
  return `${model?.id || '未知模型'}${owner}`
}

const applyLlmModels = (models) => {
  llmModels.value = normalizeLlmModels(models)
  selectedLlmModelId.value = llmModels.value.find((item) => item.selected)?.id || llmModels.value[0]?.id || ''
}

const fetchLlmModels = async () => {
  llmModelsLoading.value = true
  try {
    const response = await listLlmModels()
    if (response.data?.success) {
      applyLlmModels(response.data.data || [])
    } else {
      ElMessage.error(response.data?.message || '加载模型列表失败')
    }
  } catch (error) {
    console.error('List LLM models error:', error)
    ElMessage.error(error?.response?.data?.message || '加载模型列表失败')
  } finally {
    llmModelsLoading.value = false
  }
}

const onSelectLlmModel = async (modelId) => {
  const previousModelId = llmModels.value.find((item) => item.selected)?.id || ''
  if (!modelId || modelId === previousModelId) return

  llmModelsLoading.value = true
  try {
    const response = await selectLlmModel(modelId)
    if (!response.data?.success) {
      throw new Error(response.data?.message || '切换模型失败')
    }
    applyLlmModels(response.data.data || [])
    ElMessage.success(`已切换模型：${selectedLlmModel.value?.id || selectedLlmModelId.value}`)
  } catch (error) {
    console.error('Select LLM model error:', error)
    ElMessage.error(error?.response?.data?.message || error?.message || '切换模型失败')
    await fetchLlmModels()
  } finally {
    llmModelsLoading.value = false
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

const resetTurnVersionState = () => {
  turnVersionsLoading.value = false
  turnVersions.value = []
  selectedVersionKey.value = ''
  selectedVersionDetail.value = null
  turnVersionDetailLoading.value = false
}

const openTurnDetail = async (turnId, options = {}) => {
  if (!activeConversationId.value || !turnId) return
  const resetTab = options.resetTab !== false
  turnDetailVisible.value = true
  turnDetailLoading.value = true
  previewArtifact.value = null
  resetTurnVersionState()
  if (resetTab) turnDetailActiveTab.value = 'current'
  try {
    const response = await getTurnDetail(activeConversationId.value, turnId)
    turnDetail.value = response.data.success ? response.data.data : null
    if (!resetTab && turnDetailActiveTab.value === 'versions') {
      await loadTurnVersions(turnId)
    }
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
  artifactId = 0
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
        insight_artifact_id: artifactId || 0
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
    conversationId: activeConversation.value.id
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
    artifactId
  })
}

const toggleCurrentChartCollect = async () => {
  const primaryChart = getPrimaryChart({ charts: currentCharts.value })
  if (!currentTurnId.value || !primaryChart) return
  let artifactId = currentChartArtifactId.value || 0
  let title = `${currentQuestion.value} 图表`
  let summaryText = ''

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
    artifactId
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

const onRerunTurn = async (item) => {
  if (!activeConversationId.value || !item?.turnId) return
  stopCurrentStream()
  resetCurrentConversationState()
  currentRunMode.value = 'rerun'
  rerunSourceTurnId.value = item.turnId
  currentTurnId.value = item.turnId
  currentQuestion.value = item.question
  removeHistoryTurn(item.turnId)
  currentReport.value = ''
  currentChartArtifactId.value = 0
  loading.value = true
  addProgressItem({
    type: 'status',
    level: 'info',
    message: `正在重新执行第 ${item.turnId} 轮分析...`
  })
  scrollToBottom()

  try {
    const response = await submitRerunTurnTask(activeConversationId.value, item.turnId, clarificationEnabled.value ? 'same_turn' : 'disabled')
    const task = response.data?.data
    if (!response.data?.success || !task?.turn_id) {
      throw new Error(response.data?.message || '提交刷新分析任务失败')
    }
    currentTaskId.value = task.task_id || ''
    subscribeTurnStream(task.conversation_id, task.turn_id)
  } catch (error) {
    console.error('Submit rerun task error:', error)
    ElMessage.error(error?.message || '提交刷新分析任务失败')
    loading.value = false
  }
}

const getTurnVersionKey = (version) => (
  version?.analysis_task_id ? String(version.analysis_task_id) : 'initial'
)

const getVersionLabel = (version) => {
  if (!version) return '分析版本'
  if (version.task_type === 'initial') return '初始分析'
  if (version.task_type === 'rerun') return `刷新分析 #${version.analysis_task_id || ''}`.trim()
  if (version.task_type === 'local_rerun') return `本地执行 #${version.analysis_task_id || ''}`.trim()
  return `${version.label || '分析'}${version.analysis_task_id ? ` #${version.analysis_task_id}` : ''}`
}

const loadTurnVersionDetail = async (version) => {
  if (!activeConversationId.value || !turnDetail.value?.turn?.id || !version) return
  selectedVersionKey.value = getTurnVersionKey(version)
  selectedVersionDetail.value = null
  turnVersionDetailLoading.value = true
  try {
    const response = version.analysis_task_id
      ? await getTurnVersionDetail(activeConversationId.value, turnDetail.value.turn.id, version.analysis_task_id)
      : await getInitialTurnVersion(activeConversationId.value, turnDetail.value.turn.id)
    selectedVersionDetail.value = response.data?.success ? response.data.data : null
  } catch (error) {
    console.error('Load turn version detail error:', error)
    selectedVersionDetail.value = null
    ElMessage.error(error?.response?.data?.message || '加载分析版本失败')
  } finally {
    turnVersionDetailLoading.value = false
  }
}

const loadTurnVersions = async (turnId = turnDetail.value?.turn?.id) => {
  if (!activeConversationId.value || !turnId) return
  turnVersionsLoading.value = true
  selectedVersionDetail.value = null
  try {
    const response = await listTurnVersions(activeConversationId.value, turnId)
    const versions = response.data?.success ? response.data.data?.versions || [] : []
    turnVersions.value = versions
    if (versions.length) {
      await loadTurnVersionDetail(versions[0])
    }
  } catch (error) {
    console.error('Load turn versions error:', error)
    turnVersions.value = []
    ElMessage.error(error?.response?.data?.message || '加载分析历史失败')
  } finally {
    turnVersionsLoading.value = false
  }
}

const onTurnDetailTabChange = async (tabName) => {
  if (tabName === 'versions' && turnDetail.value?.turn?.id && !turnVersions.value.length) {
    await loadTurnVersions(turnDetail.value.turn.id)
  }
}

const selectTurnVersion = async (version) => {
  if (selectedVersionKey.value === getTurnVersionKey(version) && selectedVersionDetail.value) return
  await loadTurnVersionDetail(version)
}

const parseJsonObject = (value) => {
  if (!value) return {}
  if (typeof value === 'object' && !Array.isArray(value)) return value
  if (typeof value !== 'string') return {}
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch (error) {
    return {}
  }
}

const formatJson = (value) => JSON.stringify(value || {}, null, 2)

const hasObjectEntries = (value) => (
  value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length > 0
)

const getEvidenceDisplayValue = (detail) => {
  const context = detail?.selected_context || {}
  return context.display_value ?? context.raw_value ?? '当前数值'
}

const formatEvidenceValue = (value) => {
  if (Array.isArray(value)) return value.filter((item) => item !== null && item !== undefined && item !== '').join(' / ')
  if (value && typeof value === 'object') return JSON.stringify(value)
  return value ?? '-'
}

const stepItems = (step) => Array.isArray(step?.items)
  ? step.items.filter((item) => item && item.value !== undefined && item.value !== null && item.value !== '')
  : []

const stepWarnings = (step) => Array.isArray(step?.warnings)
  ? step.warnings.filter(Boolean)
  : []

const stepBadges = (step) => Array.isArray(step?.badges)
  ? step.badges.filter(Boolean)
  : []

const getEvidenceSteps = (detail) => Array.isArray(detail?.chain_steps) ? detail.chain_steps : []

const collectEvidenceDetails = (detail, keys) => {
  const result = {}
  getEvidenceSteps(detail).forEach((step) => {
    const details = step?.details
    if (!details || typeof details !== 'object') return
    keys.forEach((key) => {
      if (details[key] !== undefined && details[key] !== null && details[key] !== '') {
        result[`${step.title || step.type}.${key}`] = details[key]
      }
    })
  })
  return result
}

const getEvidenceSqlText = (detail) => String(detail?.debug_sql || '').trim() || '本次证据链没有记录 SQL。'

const getEvidenceCodeText = (detail) => String(detail?.generated_code || '').trim() || '本次证据链没有记录执行代码。'

const hasEvidenceDebugInfo = (detail) => Boolean(
  String(detail?.debug_sql || '').trim() || String(detail?.generated_code || '').trim()
)

const getArtifactContent = (artifact) => parseJsonObject(artifact?.content_json)

const getVersionArtifactsByType = (detail, artifactType) => (
  Array.isArray(detail?.artifacts)
    ? detail.artifacts.filter((artifact) => artifact?.artifact_type === artifactType)
    : []
)

const getVersionParameters = (detail) => (
  parseJsonObject(detail?.latest_execution?.analysis_parameters || detail?.latest_execution?.analysis_parameters_json)
)

const getVersionReport = (detail) => {
  const reportArtifact = getVersionArtifactsByType(detail, 'report')[0]
  if (reportArtifact) {
    const content = getArtifactContent(reportArtifact)
    return content.report_markdown || reportArtifact.summary_text || ''
  }
  return detail?.latest_execution?.analysis_report || ''
}

const getVersionCharts = (detail) => normalizeCharts(
  getVersionArtifactsByType(detail, 'chart').map((artifact) => {
    const content = getArtifactContent(artifact)
    return {
      id: artifact.id,
      title: artifact.title,
      summary_text: artifact.summary_text,
      chart_type: content.chart_type || '',
      chart_spec: content.chart_spec || {},
      evidence_refs: getArtifactEvidenceRefs(artifact),
      sort_no: artifact.sort_no || 0
    }
  })
)

const getVersionTables = (detail) => (
  getVersionArtifactsByType(detail, 'table')
    .map((artifact) => {
      const content = getArtifactContent(artifact)
      return {
        id: artifact.id,
        title: artifact.title,
        summary_text: artifact.summary_text,
        description: artifact.summary_text,
        columns: Array.isArray(content.columns) ? content.columns : [],
        rows: Array.isArray(content.rows) ? content.rows : [],
        evidence_refs: getArtifactEvidenceRefs(artifact),
        sort_no: artifact.sort_no || 0
      }
    })
    .sort((left, right) => (left.sort_no || 0) - (right.sort_no || 0))
)

const getExecutionParameterSchema = (execution) => (
  parseJsonObject(execution?.analysis_parameters_schema || execution?.analysis_parameters_schema_json)
)

const getExecutionParameters = (execution) => (
  parseJsonObject(execution?.analysis_parameters || execution?.analysis_parameters_json)
)

const getSchemaParameters = (schema) => (
  Array.isArray(schema?.parameters) ? schema.parameters.filter((param) => param?.name) : []
)

const hasReusableParameterSchema = (schema) => (
  schema
  && typeof schema === 'object'
  && !Array.isArray(schema)
  && Array.isArray(schema.parameters)
  && schema.defaults
  && typeof schema.defaults === 'object'
  && !Array.isArray(schema.defaults)
)

const hasLocalRunnableExecution = (execution) => hasRunnableExecution(execution)

const canLocalRunTurn = (item) => Boolean(item?.turnId && hasLocalRunnableExecution(item.latestExecution))

const getLocalRunDisabledReason = (item) => {
  const execution = item?.latestExecution
  if (!execution?.id) return '当前轮次没有可复用的执行记录'
  if (execution.execution_status && execution.execution_status !== 'success') return '只有成功的执行记录可以本地执行'
  const schema = getExecutionParameterSchema(execution)
  if (!hasRunnableExecution(execution)) return '当前执行记录缺少可复用分析代码'
  if (!hasReusableParameterSchema(schema)) return '当前分析使用固定口径，将直接本地执行'
  if (!getSchemaParameters(schema).length) return '当前分析没有可调整参数，将按固定口径本地执行'
  return '使用当前参数在本地重新执行'
}

const buildDefaultLocalParameters = (execution) => {
  const schema = getExecutionParameterSchema(execution)
  return {
    ...parseJsonObject(schema.defaults),
    ...getExecutionParameters(execution)
  }
}

const promptLocalExecutionParameters = async (execution) => {
  const defaultParams = buildDefaultLocalParameters(execution)
  const { value } = await ElMessageBox.prompt(
    '可调整本次本地执行参数，保持 JSON 对象格式。',
    '本地执行参数',
    {
      inputType: 'textarea',
      inputValue: JSON.stringify(defaultParams, null, 2),
      inputValidator: (rawValue) => {
        try {
          const parsed = JSON.parse(rawValue || '{}')
          return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        } catch (error) {
          return false
        }
      },
      inputErrorMessage: '请输入合法的 JSON 对象',
      confirmButtonText: '开始执行',
      cancelButtonText: '取消'
    }
  )
  return JSON.parse(value || '{}')
}

const getTurnDetailLocalRunItem = () => ({
  turnId: turnDetail.value?.turn?.id || 0,
  question: turnDetail.value?.turn?.user_query || '',
  report: turnDetail.value?.turn?.final_answer || '',
  latestExecution: turnDetail.value?.latest_execution || null
})

const refreshAfterLocalRerun = async (turnId) => {
  const shouldReloadVersions = turnDetailVisible.value && turnDetail.value?.turn?.id === turnId && turnDetailActiveTab.value === 'versions'
  await Promise.all([fetchConversations(), fetchCollects()])
  if (activeConversationId.value) await loadConversationHistory(activeConversationId.value)
  if (turnDetailVisible.value && turnDetail.value?.turn?.id === turnId) {
    await openTurnDetail(turnId, { resetTab: !shouldReloadVersions })
  }
}

const onLocalRerunTurn = async (item) => {
  if (!activeConversationId.value || !item?.turnId || localRerunLoading.value) return
  const execution = item.latestExecution
  if (!hasLocalRunnableExecution(execution)) {
    ElMessage.warning(getLocalRunDisabledReason(item))
    return
  }

  try {
    const analysisParameters = await promptLocalExecutionParameters(execution)
    localRerunLoading.value = true
    localRerunTurnId.value = item.turnId
    const response = await rerunExecutionLocally(
      activeConversationId.value,
      item.turnId,
      execution.id,
      analysisParameters
    )
    const data = response.data?.data || {}
    if (!response.data?.success || (data.status && data.status !== 'success')) {
      throw new Error(response.data?.message || data.error_message || '本地执行失败')
    }
    ElMessage.success('本地执行完成')
    await refreshAfterLocalRerun(item.turnId)
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    console.error('Local rerun error:', error)
    ElMessage.error(error?.response?.data?.message || error?.message || '本地执行失败')
  } finally {
    localRerunLoading.value = false
    localRerunTurnId.value = 0
  }
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
    await resumeRunningTurnIfAny(conversations.value[0].id)
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
  await resumeRunningTurnIfAny(conversation.id)
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
  const finishedConversationId = activeConversationId.value
  loading.value = false
  currentStreamController.value = null
  finalizeCurrentConversation()
  await Promise.all([fetchConversations(), fetchCollects()])
  if (finishedConversationId) {
    await loadConversationHistory(finishedConversationId)
  }
}

const handleStreamEvent = async (event) => {
  if (!event || typeof event !== 'object') return
  if (event.conversation_id) activeConversationId.value = Number(event.conversation_id)
  if (event.task_id) currentTaskId.value = String(event.task_id)
  if (event.turn_id) currentTurnId.value = Number(event.turn_id)

  if (event.type === 'session') {
    await Promise.all([fetchConversations(), fetchCollects()])
    return
  }
  if (event.type === 'result') {
      currentCharts.value = normalizeCharts(event.charts || [])
      if (Array.isArray(event.tables)) currentTables.value = normalizeTables(event.tables)
      if (event.chart_artifact_id) currentChartArtifactId.value = Number(event.chart_artifact_id)
      if (event.analysis_report) currentReport.value = event.analysis_report
      if (event.report_evidence_refs) currentReportRefs.value = event.report_evidence_refs
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
  if (event.type === 'clarification_required') {
    currentClarification.value = event.clarification || null
    clarificationAnswers.value = {}
    currentStreamController.value = null
    addProgressItem({
      type: 'status',
      level: 'info',
      message: '需要补充一个关键分析口径后继续。'
    })
    scrollToBottom()
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
  if (currentTaskId.value) {
    try {
      const response = await getAnalysisTask(currentTaskId.value)
      const task = response.data?.data
      if (['queued', 'running', 'waiting_clarification'].includes(task?.status)) {
        addProgressItem({ type: 'status', level: 'warning', message: '实时连接中断，正在尝试重新订阅...' })
        subscribeTurnStream(task.conversation_id, task.turn_id)
        return
      }
      await finalizeStreamRound()
      return
    } catch (taskError) {
      console.error('Query task after stream error failed:', taskError)
    }
  }
  ElMessage.error(`请求失败: ${error.message || '未知错误'}`)
  addProgressItem({ type: 'error', level: 'error', message: '请求失败，无法获取实时分析结果。' })
  if (!currentReport.value) currentReport.value = '请求失败了。'
  await finalizeStreamRound()
}

const handleStreamDone = async () => {
  if (!loading.value) return
  if (currentClarification.value) return
  if (!currentReport.value && currentAssistantMessage.value) {
    currentReport.value = currentAssistantMessage.value
  } else if (!currentReport.value && currentCharts.value.length === 0) {
    currentReport.value = '本轮分析未生成可展示的图表或分析报告，请重试。'
  }
  await finalizeStreamRound()
}

const submitClarificationAnswer = async () => {
  if (clarificationSubmitting.value) return
  if (!currentTaskId.value || !currentClarification.value) return
  if (!clarificationReady.value) return
  const answers = clarificationItems.value.map((item) => ({
    item_id: item.item_id,
    option_id: clarificationDraft(item).option_id,
    answer_text: clarificationDraft(item).answer_text.trim()
  }))
  const singleAnswer = answers.length === 1 ? answers[0] : null
  clarificationSubmitting.value = true
  try {
    const response = await submitTaskClarification(currentTaskId.value, {
      clarification_id: currentClarification.value.clarification_id,
      option_id: singleAnswer?.option_id || '',
      answer_text: singleAnswer?.answer_text || '',
      answers: answers.length > 1 ? answers : undefined
    })
    const task = response.data?.data
    if (!response.data?.success || !task?.turn_id) {
      throw new Error(response.data?.message || '提交澄清回答失败')
    }
    currentClarification.value = null
    clarificationAnswers.value = {}
    addProgressItem({
      type: 'status',
      level: 'info',
      message: '已补充分析口径，正在继续本轮分析。'
    })
    subscribeTurnStream(task.conversation_id, task.turn_id)
  } catch (error) {
    ElMessage.error(error?.message || '提交澄清回答失败')
  } finally {
    clarificationSubmitting.value = false
  }
}

const onSendMessage = async (content) => {
  if (!content.trim()) return
  if (currentClarification.value) {
    if (clarificationSubmitting.value) return
    if (clarificationItems.value.length !== 1 || !clarificationItems.value[0].allow_free_text) {
      ElMessage.info('请在上方澄清区域逐项补充或选择答案')
      return
    }
    updateClarificationText(clarificationItems.value[0], content.trim())
    await submitClarificationAnswer()
    return
  }
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

  try {
    const response = await submitAgentTask({
      namespace_id: activeNamespace.value,
      conversation_id: activeConversationId.value || '',
      user_message: content,
      clarification_mode: clarificationEnabled.value ? 'same_turn' : 'disabled',
      datasource: currentDataSource.value
    })
    const task = response.data?.data
    if (!response.data?.success || !task?.turn_id) {
      throw new Error(response.data?.message || '提交分析任务失败')
    }
    activeConversationId.value = Number(task.conversation_id)
    currentTaskId.value = task.task_id || ''
    currentTurnId.value = Number(task.turn_id)
    if (task.status === 'queued') {
      addProgressItem({
        type: 'status',
        level: 'info',
        message: '问题已进入分析队列，等待执行...'
      })
    }
    subscribeTurnStream(task.conversation_id, task.turn_id)
  } catch (error) {
    console.error('Submit analysis task error:', error)
    ElMessage.error(error?.message || '提交分析任务失败')
    loading.value = false
  }
}

const onStopAnalysis = async () => {
  if ((!loading.value && !currentClarification.value) || !currentTaskId.value) return
  try {
    await cancelAnalysisTask(currentTaskId.value)
    currentClarification.value = null
    clarificationAnswers.value = {}
    stopCurrentStream()
    addProgressItem({
      type: 'status',
      level: 'warning',
      message: '用户已停止本轮分析。'
    })
    currentReport.value = '用户已停止本轮分析。'
    ElMessage.success('已停止分析')
    await finalizeStreamRound()
  } catch (error) {
    console.error('Cancel analysis task error:', error)
    ElMessage.error(error?.message || '停止分析失败')
  }
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

watch(insightTaskPanelVisible, (visible) => {
  if (visible) scheduleInsightTaskPolling()
  else {
    insightTaskDetailRequestId += 1
    insightTaskRunRequestId += 1
    stopInsightTaskPolling()
  }
})

onMounted(async () => {
  await Promise.all([fetchLlmModels(), fetchSpaces(), fetchInsightTasks()])
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

onUnmounted(() => {
  stopInsightTaskPolling()
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
.model-switcher { height: 34px; display: flex; align-items: center; gap: 8px; padding: 0 10px; border-radius: 12px; background: rgba(255, 255, 255, 0.08); }
.model-label { font-size: 12px; color: rgba(255, 255, 255, 0.72); white-space: nowrap; }
.model-select { width: 230px; }
.model-switcher :deep(.el-select__wrapper) { min-height: 26px; border: none; box-shadow: none; background: rgba(255, 255, 255, 0.96); border-radius: 8px; }
.model-switcher :deep(.el-select__selected-item) { max-width: 178px; font-size: 12px; color: #0f172a; }
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
.action-btn:disabled { cursor: not-allowed; opacity: 0.52; background: #f8fafc; }
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
.progress-item { padding: 10px 12px; border-radius: 12px; font-size: 13px; line-height: 1.6; white-space: pre-wrap; display: flex; flex-direction: column; gap: 2px; }
.progress-item strong { font-size: 12px; }
.progress-item.info { background: #eef5ff; color: #245b9f; }
.progress-item.success { background: #edf9f0; color: #2f7d4d; }
.progress-item.warning { background: #fff7e8; color: #9a6700; }
.progress-item.error { background: #fff0f0; color: #c45656; }
.progress-item.assistant { background: #f5f3ff; color: #5b45b0; }
.progress-empty { font-size: 12px; color: #64748b; }
.clarification-card { max-width: 720px; padding: 16px; border: 1px solid #bfdbfe; border-radius: 8px; background: #f8fbff; }
.clarification-title { color: #1d4ed8; font-weight: 700; }
.clarification-item { padding-top: 12px; }
.clarification-item + .clarification-item { margin-top: 14px; border-top: 1px solid #dbeafe; }
.clarification-question { margin-top: 8px; color: #0f172a; line-height: 1.7; }
.clarification-options { display: grid; gap: 8px; margin-top: 12px; }
.clarification-option { display: flex; flex-direction: column; gap: 3px; padding: 10px 12px; border: 1px solid #dbeafe; border-radius: 6px; background: #fff; color: #1e3a8a; text-align: left; cursor: pointer; }
.clarification-option:hover { border-color: #60a5fa; background: #eff6ff; }
.clarification-option.selected { border-color: #2563eb; background: #eff6ff; box-shadow: 0 0 0 1px #2563eb; }
.clarification-option span { color: #64748b; font-size: 12px; }
.clarification-free-text { display: flex; gap: 8px; margin-top: 12px; }
.clarification-free-text .el-input { flex: 1; }
.clarification-actions { display: flex; justify-content: flex-end; margin-top: 16px; }
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
.answer-report :deep(.traceable-value) {
  cursor: pointer;
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 3px;
}
.answer-report :deep(.traceable-value:hover) {
  color: #1d4ed8;
}
.traceable-cell {
  cursor: pointer;
}
.traceable-cell:hover {
  background: #eff6ff;
}
.evidence-panel { display: flex; flex-direction: column; gap: 18px; padding-bottom: 18px; }
.evidence-hero { padding: 0 0 14px; border-bottom: 1px solid #e5e7eb; }
.evidence-hero-title { font-size: 20px; line-height: 1.35; font-weight: 800; color: #0f172a; word-break: break-word; }
.evidence-hero-subtitle { margin-top: 6px; font-size: 13px; color: #475569; }
.evidence-chain { display: flex; flex-direction: column; }
.evidence-step { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 12px; position: relative; }
.evidence-step:not(:last-child) { padding-bottom: 16px; }
.evidence-step-rail { position: relative; display: flex; justify-content: center; }
.evidence-step:not(:last-child) .evidence-step-rail::after {
  content: '';
  position: absolute;
  top: 30px;
  bottom: -16px;
  width: 2px;
  background: #0f8b8d;
}
.evidence-step-index {
  position: relative;
  z-index: 1;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: #0f8b8d;
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 800;
}
.evidence-step-body { border-bottom: 1px solid #e5e7eb; padding: 0 0 14px; min-width: 0; }
.evidence-step-final .evidence-step-body { border-bottom: none; }
.evidence-step-title { font-size: 15px; font-weight: 800; color: #0f172a; margin-bottom: 8px; }
.evidence-step-summary { font-size: 13px; line-height: 1.7; color: #334155; }
.evidence-step-items { display: flex; flex-direction: column; gap: 7px; margin-top: 10px; }
.evidence-step-item { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 12px; font-size: 13px; line-height: 1.55; }
.evidence-item-label { color: #64748b; }
.evidence-item-value { color: #0f172a; font-weight: 600; word-break: break-word; }
.evidence-formula {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #f0fdfa;
  border: 1px solid #99f6e4;
  color: #0f766e;
  font-size: 18px;
  font-weight: 800;
  text-align: center;
  word-break: break-word;
}
.evidence-method {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid #bae6fd;
  background: #f0f9ff;
  border-radius: 8px;
}
.evidence-method-label { color: #0369a1; font-size: 12px; font-weight: 800; margin-bottom: 4px; }
.evidence-method-value { color: #0f172a; font-size: 13px; line-height: 1.6; font-weight: 600; }
.evidence-output {
  display: inline-flex;
  margin-top: 10px;
  color: #0f8b8d;
  font-weight: 800;
  font-size: 13px;
}
.evidence-warnings { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.evidence-warning { color: #d97706; font-size: 13px; line-height: 1.6; }
.evidence-step-badges { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
.evidence-result-card {
  margin-top: 14px;
  border: 1px solid #99f6e4;
  background: linear-gradient(180deg, #f0fdfa 0%, #ffffff 100%);
  border-radius: 8px;
  padding: 18px 14px;
  text-align: center;
}
.evidence-result-value { color: #0f8b8d; font-size: 42px; line-height: 1.1; font-weight: 800; word-break: break-word; }
.evidence-result-label { margin-top: 6px; color: #334155; font-size: 14px; font-weight: 700; }
.evidence-result-badges { margin-top: 10px; display: flex; justify-content: center; flex-wrap: wrap; gap: 8px; }
.evidence-badge { border: 1px solid #86efac; color: #15803d; background: #f0fdf4; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 700; }
.evidence-debug-collapse { border-top: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb; }
.evidence-debug-pre {
  margin: 0;
  padding: 10px;
  border-radius: 8px;
  background: #f8fafc;
  color: #334155;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre-wrap;
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
.detail-tabs :deep(.el-tabs__content) { overflow: visible; }
.version-layout { display: grid; grid-template-columns: minmax(180px, 220px) minmax(0, 1fr); gap: 14px; align-items: start; }
.version-list { display: flex; flex-direction: column; gap: 8px; }
.version-item { width: 100%; border: 1px solid #e5edf7; background: #fff; border-radius: 10px; padding: 10px; text-align: left; cursor: pointer; color: #334155; }
.version-item:hover, .version-item.active { border-color: #60a5fa; background: #f8fbff; }
.version-item.current { box-shadow: inset 3px 0 0 #2563eb; }
.version-item-main { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 700; color: #0f172a; }
.version-item-meta { display: flex; justify-content: space-between; gap: 8px; margin-top: 6px; font-size: 11px; color: #64748b; }
.version-preview, .version-content { min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.version-summary { margin-bottom: 2px; }
.version-json { margin: 0; overflow-x: auto; font-size: 12px; white-space: pre; }
.version-empty { min-height: 120px; border: 1px dashed #dbe3ef; border-radius: 12px; display: flex; align-items: center; justify-content: center; background: #f8fbff; }
.task-workspace { min-height: calc(100vh - 150px); display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 18px; }
.task-list-pane { padding-right: 16px; border-right: 1px solid #e5edf7; overflow-y: auto; }
.task-detail-pane { min-width: 0; overflow-y: auto; }
.task-pane-header, .task-detail-header, .task-card-title-row, .task-card-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.task-pane-header, .task-detail-header { margin-bottom: 16px; }
.task-card { width: 100%; margin-bottom: 10px; padding: 14px; border: 1px solid #e5edf7; border-radius: 8px; background: #fff; color: #334155; text-align: left; cursor: pointer; }
.task-card:hover, .task-card.active { border-color: #3b82f6; background: #f8fbff; }
.task-card-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; font-weight: 700; color: #0f172a; }
.task-card-schedule { margin-top: 10px; font-size: 12px; color: #475569; }
.task-schedule-hint { margin-left: 12px; font-size: 12px; color: #64748b; }
.task-card-meta { margin-top: 10px; font-size: 11px; color: #64748b; }
.task-empty { min-height: 180px; }
.task-caliber-section { padding: 0; overflow: hidden; }
.task-caliber-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; background: #f8fafc; cursor: pointer; list-style: none; user-select: none; }
.task-caliber-head::-webkit-details-marker { display: none; }
.task-caliber-head::after { content: '⌄'; margin-left: 6px; font-size: 14px; color: #64748b; }
.task-caliber-section[open] .task-caliber-head { border-bottom: 1px solid #e5edf7; }
.task-caliber-section[open] .task-caliber-head::after { content: '⌃'; }
.task-caliber-meta { font-size: 11px; color: #64748b; }
.task-caliber-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 24px; padding: 14px 16px; }
.task-caliber-item { min-width: 0; }
.task-caliber-item.wide { grid-column: 1 / -1; }
.task-caliber-item label { display: block; margin-bottom: 3px; font-size: 11px; color: #64748b; }
.task-caliber-item span { display: block; overflow-wrap: anywhere; font-size: 13px; line-height: 1.55; color: #334155; }
.task-datasource-list { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
.task-schedule-summary { margin-top: 10px; font-size: 14px; font-weight: 700; color: #0f172a; }
.task-run-list { display: flex; flex-direction: column; gap: 8px; }
.task-run-item { display: grid; grid-template-columns: minmax(90px, 1fr) auto minmax(150px, 1fr) 70px; align-items: center; gap: 10px; width: 100%; padding: 11px 12px; border: 1px solid #e5edf7; border-radius: 8px; background: #fff; color: #334155; text-align: left; cursor: pointer; }
.task-run-item:hover, .task-run-item.active { border-color: #60a5fa; background: #f8fbff; }
.task-run-error { margin-top: 12px; padding: 12px; border-radius: 8px; background: #fff1f2; color: #be123c; font-size: 13px; line-height: 1.6; }
.task-load-more { align-self: center; margin-top: 4px; }
.task-detail-sections { display: flex; flex-direction: column; gap: 18px; }
.task-detail-section { min-width: 0; padding: 18px; border: 1px solid #e5edf7; border-radius: 8px; background: #fff; }
.task-section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid #eef2f7; }
@media (max-width: 1180px) { .version-layout { grid-template-columns: 1fr; } }
@media (max-width: 960px) { .task-workspace, .task-caliber-grid { grid-template-columns: 1fr; } .task-caliber-item.wide { grid-column: auto; } .task-list-pane { border-right: none; border-bottom: 1px solid #e5edf7; padding: 0 0 14px; } }
@media (max-width: 1440px) { .result-card { margin-left: 0; } }
</style>
