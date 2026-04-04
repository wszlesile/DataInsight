<template>
  <div class="sidebar">
    <div class="sidebar-header">
      <div class="logo">
        <span class="logo-icon">DI</span>
        <span class="logo-text">DataInsight</span>
      </div>
    </div>

    <div class="sidebar-content">
      <el-button type="primary" class="new-insight-btn" @click="$emit('new-space')">
        <span>+</span> 新建洞察
      </el-button>

      <div class="nav-section">
        <div class="nav-title">洞察空间</div>
        <div
          v-for="space in spaces"
          :key="space.id"
          class="nav-item"
          :class="{ active: activeSpace === space.id }"
          @click="$emit('select-space', space)"
        >
          <span class="nav-icon">NS</span>
          <span class="nav-text">{{ space.name }}</span>
          <el-button
            size="small"
            text
            class="space-delete-btn"
            @click.stop="$emit('delete-space', space)"
          >
            删除
          </el-button>
        </div>
      </div>

      <div class="nav-section">
        <div class="nav-title">会话列表</div>
        <div v-if="conversations.length === 0" class="sidebar-empty">当前空间还没有历史会话</div>
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ active: activeConversationId === conversation.id }"
          @click="$emit('select-conversation', conversation)"
        >
          <div class="conversation-row">
            <div class="conversation-title">{{ conversation.title || `会话 ${conversation.id}` }}</div>
            <el-button
              size="small"
              text
              class="rename-btn"
              @click.stop="$emit('rename-conversation', conversation)"
            >
              重命名
            </el-button>
          </div>
          <div v-if="conversation.summary_text" class="conversation-summary">
            {{ conversation.summary_text }}
          </div>
          <div v-if="conversation.last_message_at" class="conversation-time">
            {{ formatTime(conversation.last_message_at) }}
          </div>
        </div>
      </div>

      <div class="nav-section">
        <div class="nav-title">我的收藏</div>
        <div class="collect-toolbar">
          <el-input v-model="collectKeyword" size="small" clearable placeholder="搜索收藏" />
          <el-select v-model="collectTypeFilter" size="small">
            <el-option label="全部" value="all" />
            <el-option label="会话" value="conversation" />
            <el-option label="轮次" value="turn" />
            <el-option label="产物" value="artifact" />
          </el-select>
        </div>

        <div v-if="filteredCollectGroups.length === 0" class="sidebar-empty">
          当前空间还没有符合条件的收藏
        </div>

        <div v-for="group in filteredCollectGroups" :key="group.type" class="collect-group">
          <div class="collect-group-title">{{ collectTypeLabel(group.type) }}</div>
          <div
            v-for="collect in group.items"
            :key="collect.id"
            class="collect-item"
            @click="$emit('select-collect', collect)"
          >
            <div class="collect-title">{{ collect.title || `收藏 ${collect.id}` }}</div>
            <div v-if="collect.summary_text" class="collect-summary">{{ collect.summary_text }}</div>
            <div class="collect-meta">ID: {{ collect.target_id }}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  spaces: { type: Array, default: () => [] },
  activeSpace: { type: String, default: '' },
  conversations: { type: Array, default: () => [] },
  collects: { type: Array, default: () => [] },
  activeConversationId: { type: Number, default: 0 }
})

defineEmits([
  'select-space',
  'delete-space',
  'select-conversation',
  'rename-conversation',
  'new-space',
  'select-collect'
])

const collectKeyword = ref('')
const collectTypeFilter = ref('all')

const collectTypeLabel = (type) => ({
  conversation: '会话收藏',
  turn: '轮次收藏',
  artifact: '产物收藏'
}[type] || '其他收藏')

const filteredCollectGroups = computed(() => {
  const keyword = collectKeyword.value.trim().toLowerCase()
  const filtered = props.collects.filter((item) => {
    const typeMatched = collectTypeFilter.value === 'all' || item.collect_type === collectTypeFilter.value
    if (!typeMatched) return false
    if (!keyword) return true
    const haystack = `${item.title || ''} ${item.summary_text || ''}`.toLowerCase()
    return haystack.includes(keyword)
  })

  const groups = new Map()
  filtered.forEach((item) => {
    if (!groups.has(item.collect_type)) groups.set(item.collect_type, [])
    groups.get(item.collect_type).push(item)
  })

  return Array.from(groups.entries()).map(([type, items]) => ({ type, items }))
})

const formatTime = (value) => {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
</script>

<style scoped>
.sidebar { width: 300px; height: 100vh; background: #1a1a2e; color: #fff; display: flex; flex-direction: column; }
.sidebar-header { padding: 20px; border-bottom: 1px solid rgba(255, 255, 255, 0.1); }
.logo { display: flex; align-items: center; gap: 10px; }
.logo-icon { width: 30px; height: 30px; border-radius: 8px; display: inline-flex; align-items: center; justify-content: center; background: rgba(74, 144, 226, 0.25); font-size: 12px; font-weight: 700; }
.logo-text { font-size: 18px; font-weight: 700; }
.sidebar-content { flex: 1; padding: 16px; overflow-y: auto; }
.new-insight-btn { width: 100%; margin-bottom: 24px; background: #4a90e2; border: none; }
.nav-section { margin-bottom: 24px; }
.nav-title { padding: 8px 12px; font-size: 12px; color: rgba(255, 255, 255, 0.55); text-transform: uppercase; }
.nav-item, .conversation-item, .collect-item { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border-radius: 10px; cursor: pointer; transition: background 0.2s; }
.nav-item { flex-direction: row; align-items: center; gap: 8px; }
.nav-item:hover, .conversation-item:hover, .collect-item:hover { background: rgba(255, 255, 255, 0.08); }
.nav-item.active, .conversation-item.active { background: rgba(74, 144, 226, 0.28); }
.nav-icon { width: 24px; color: rgba(255, 255, 255, 0.7); font-size: 12px; }
.nav-text, .conversation-title, .collect-title { font-size: 13px; font-weight: 600; color: #fff; }
.space-delete-btn { margin-left: auto; color: rgba(255, 255, 255, 0.72); }
.conversation-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.rename-btn { color: rgba(255, 255, 255, 0.75); }
.conversation-summary, .collect-summary { font-size: 12px; line-height: 1.5; color: rgba(255, 255, 255, 0.65); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.conversation-time, .collect-meta, .collect-group-title { font-size: 11px; color: rgba(255, 255, 255, 0.45); }
.collect-toolbar { display: flex; flex-direction: column; gap: 8px; padding: 0 12px 12px; }
.collect-group { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.collect-group-title { padding: 0 12px; text-transform: uppercase; }
.sidebar-empty { padding: 10px 12px; font-size: 12px; color: rgba(255, 255, 255, 0.5); }
</style>
