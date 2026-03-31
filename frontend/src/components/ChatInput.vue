<template>
  <div class="chat-input">
    <div class="input-wrapper">
      <el-input
        v-model="message"
        type="textarea"
        :rows="3"
        placeholder="输入你的数据分析问题..."
        @keydown.enter.ctrl="sendMessage"
      />
      <div class="input-actions">
        <span class="hint">Ctrl + Enter 发送</span>
        <el-button
          type="primary"
          :loading="loading"
          :disabled="!message.trim()"
          @click="sendMessage"
        >
          {{ loading ? '分析中...' : '发送' }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
  loading: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['send'])

const message = ref('')

const sendMessage = () => {
  if (message.value.trim() && !props.loading) {
    emit('send', message.value)
  }
}
</script>

<style scoped>
.chat-input {
  padding: 16px;
  background: #fff;
  border-top: 1px solid #e4e7ed;
}

.input-wrapper {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.input-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.hint {
  font-size: 12px;
  color: #909399;
}
</style>