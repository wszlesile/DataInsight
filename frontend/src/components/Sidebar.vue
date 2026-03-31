<template>
  <div class="sidebar">
    <div class="sidebar-header">
      <div class="logo">
        <span class="logo-icon">📊</span>
        <span class="logo-text">DataInsight</span>
      </div>
    </div>

    <div class="sidebar-content">
      <el-button type="primary" class="new-insight-btn">
        <span>+</span> 新建洞察
      </el-button>

      <div class="nav-section">
        <div class="nav-title">洞察空间</div>
        <div
          v-for="space in insightSpaces"
          :key="space.id"
          class="nav-item"
          :class="{ active: activeSpace === space.id }"
          @click="selectSpace(space)"
        >
          <span class="nav-icon">📁</span>
          <span class="nav-text">{{ space.name }}</span>
        </div>
      </div>

      <div class="nav-section">
        <div class="nav-title">知识库</div>
        <div class="nav-item" @click="$emit('navigate', 'knowledge')">
          <span class="nav-icon">📚</span>
          <span class="nav-text">知识库管理</span>
        </div>
      </div>

      <div class="nav-section">
        <div class="nav-title">数据源</div>
        <div class="nav-item" @click="$emit('navigate', 'datasource')">
          <span class="nav-icon">🗄️</span>
          <span class="nav-text">数据源管理</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const emit = defineEmits(['select-space', 'navigate'])

const insightSpaces = ref([
  { id: '1', name: '销售数据分析' },
  { id: '2', name: '报警数据分析' },
  { id: '3', name: '用户行为分析' }
])

const activeSpace = ref('1')

const selectSpace = (space) => {
  activeSpace.value = space.id
  emit('select-space', space)
}
</script>

<style scoped>
.sidebar {
  width: 240px;
  height: 100vh;
  background: #1a1a2e;
  color: #fff;
  display: flex;
  flex-direction: column;
}

.sidebar-header {
  padding: 20px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo-icon {
  font-size: 24px;
}

.logo-text {
  font-size: 18px;
  font-weight: bold;
}

.sidebar-content {
  flex: 1;
  padding: 16px;
  overflow-y: auto;
}

.new-insight-btn {
  width: 100%;
  margin-bottom: 24px;
  background: #4a90e2;
  border: none;
}

.new-insight-btn:hover {
  background: #3a7bc8;
}

.nav-section {
  margin-bottom: 24px;
}

.nav-title {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  padding: 8px 12px;
  text-transform: uppercase;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.2s;
}

.nav-item:hover {
  background: rgba(255, 255, 255, 0.1);
}

.nav-item.active {
  background: rgba(74, 144, 226, 0.3);
}

.nav-icon {
  font-size: 16px;
}

.nav-text {
  font-size: 14px;
}
</style>