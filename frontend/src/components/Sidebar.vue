<template>
  <aside class="left-panel" :class="{ collapsed }">
    <div class="panel-section">
      <div class="panel-header">
        <span v-if="!collapsed" class="panel-title">📚 洞察空间</span>
        <div class="panel-actions">
          <button class="panel-icon-btn" type="button" title="新建洞察" @click="$emit('new-space')">
            +
          </button>
          <button
            class="panel-icon-btn"
            type="button"
            :title="collapsed ? '展开侧边栏' : '折叠侧边栏'"
            @click="collapsed = !collapsed"
          >
            {{ collapsed ? '›' : '‹' }}
          </button>
        </div>
      </div>

      <div class="notebook-list">
        <div
          v-for="space in spaces"
          :key="space.id"
          class="notebook-item"
          :class="{ active: activeSpace === String(space.id) }"
          @click="$emit('select-space', space)"
        >
          <div class="notebook-icon" :style="{ background: notebookGradient(space.id) }">
            {{ collapsed ? 'DI' : notebookAbbr(space.name) }}
          </div>

          <div v-if="!collapsed" class="notebook-info">
            <div class="notebook-name">{{ space.name }}</div>
            <div class="notebook-count">
              {{ activeSpace === String(space.id) ? currentConversationHint : '点击进入洞察' }}
            </div>
          </div>

          <button
            v-if="!collapsed"
            class="notebook-more"
            type="button"
            title="重命名空间"
            @click.stop="$emit('rename-space', space)"
          >
            重命名
          </button>

          <button
            v-if="!collapsed"
            class="notebook-more danger"
            type="button"
            title="删除空间"
            @click.stop="$emit('delete-space', space)"
          >
            删除
          </button>
        </div>
      </div>

      <div class="panel-footer-links">
        <div class="notebook-item footer-item" @click="$emit('open-knowledge')">
          <div class="notebook-icon footer-icon knowledge">KB</div>
          <div v-if="!collapsed" class="notebook-info">
            <div class="notebook-name">知识库</div>
            <div class="notebook-count">后续接入统一资源</div>
          </div>
        </div>

        <div class="notebook-item footer-item" @click="$emit('open-favorites')">
          <div class="notebook-icon footer-icon favorite">★</div>
          <div v-if="!collapsed" class="notebook-info">
            <div class="notebook-name">我的收藏</div>
            <div class="notebook-count">{{ collects.length }} 个收藏</div>
          </div>
          <span v-if="!collapsed && collects.length" class="favorites-badge">{{ collects.length }}</span>
        </div>
      </div>
    </div>
  </aside>
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
  'rename-space',
  'new-space',
  'open-favorites',
  'open-knowledge'
])

const collapsed = ref(false)

const currentConversationHint = computed(() => {
  if (!props.activeConversationId) return '新会话已创建'
  const activeConversation = props.conversations.find((item) => item.id === props.activeConversationId)
  if (!activeConversation) return '会话已就绪'
  return activeConversation.title || `会话 #${activeConversation.id}`
})

const gradients = [
  'linear-gradient(135deg, #f59e0b, #ef4444)',
  'linear-gradient(135deg, #3b82f6, #1d4ed8)',
  'linear-gradient(135deg, #10b981, #059669)',
  'linear-gradient(135deg, #8b5cf6, #6d28d9)',
  'linear-gradient(135deg, #ec4899, #db2777)',
  'linear-gradient(135deg, #f97316, #ea580c)'
]

const notebookGradient = (id) => gradients[Number(id) % gradients.length]

const notebookAbbr = (name) => {
  if (!name) return 'DI'
  return String(name).slice(0, 2).toUpperCase()
}
</script>

<style scoped>
.left-panel {
  width: 280px;
  min-width: 280px;
  background: #ffffff;
  border-right: 1px solid #dbe3ef;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.3s ease, min-width 0.3s ease;
}

.left-panel.collapsed {
  width: 72px;
  min-width: 72px;
}

.panel-section {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 18px 16px 12px;
  background:
    radial-gradient(circle at top left, rgba(37, 99, 235, 0.1), transparent 32%),
    linear-gradient(180deg, #f9fbff 0%, #ffffff 58%);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 18px;
}

.panel-title {
  font-size: 14px;
  font-weight: 700;
  color: #4b5563;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-icon-btn {
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 9px;
  background: #eef4ff;
  color: #52637d;
  cursor: pointer;
  font-size: 16px;
  transition: background 0.2s ease, color 0.2s ease, transform 0.2s ease;
}

.panel-icon-btn:hover {
  background: #dce9ff;
  color: #1d4ed8;
  transform: translateY(-1px);
}

.notebook-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-right: 2px;
}

.notebook-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: 14px;
  cursor: pointer;
  transition: transform 0.2s ease, background 0.2s ease, box-shadow 0.2s ease;
}

.notebook-item:hover {
  background: #f2f7ff;
  transform: translateX(2px);
}

.notebook-item.active {
  background: linear-gradient(135deg, rgba(37, 99, 235, 0.14), rgba(14, 165, 233, 0.08));
  box-shadow: inset 0 0 0 1px rgba(59, 130, 246, 0.18);
}

.notebook-icon {
  width: 40px;
  height: 40px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.notebook-info {
  min-width: 0;
  flex: 1;
}

.notebook-name {
  font-size: 14px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.35;
}

.notebook-count {
  margin-top: 4px;
  font-size: 12px;
  color: #64748b;
  line-height: 1.45;
}

.notebook-more {
  border: none;
  background: transparent;
  color: #94a3b8;
  cursor: pointer;
  font-size: 12px;
  opacity: 0;
  transition: opacity 0.2s ease, color 0.2s ease;
}

.danger {
  color: #c2410c;
}

.notebook-item:hover .notebook-more,
.notebook-item.active .notebook-more {
  opacity: 1;
}

.notebook-more:hover {
  color: #dc2626;
}

.panel-footer-links {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  margin-top: 12px;
  border-top: 1px solid #e5edf7;
}

.footer-item {
  background: #f8fbff;
}

.footer-icon.knowledge {
  background: linear-gradient(135deg, #06b6d4, #0891b2);
}

.footer-icon.favorite {
  background: linear-gradient(135deg, #ec4899, #db2777);
}

.favorites-badge {
  position: absolute;
  top: 10px;
  right: 10px;
  min-width: 18px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: #ef4444;
  color: #fff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
}

.left-panel.collapsed .panel-section {
  padding: 18px 10px 12px;
}

.left-panel.collapsed .panel-header {
  flex-direction: column;
  align-items: center;
}

.left-panel.collapsed .panel-actions {
  flex-direction: column;
}

.left-panel.collapsed .notebook-item {
  justify-content: center;
  padding: 10px 8px;
}

.left-panel.collapsed .panel-footer-links {
  margin-top: auto;
}
</style>
