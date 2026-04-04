<template>
  <div class="chat-input-area">
    <div class="chat-input-container">
      <textarea
        v-model="message"
        class="chat-input"
        rows="1"
        placeholder="请输入你想了解的数据问题..."
        @keydown.enter.exact.prevent="sendMessage"
        @keydown.enter.ctrl.prevent="sendMessage"
      />
      <button
        class="chat-send-btn"
        type="button"
        :disabled="loading || !message.trim()"
        @click="sendMessage"
      >
        <span v-if="loading">…</span>
        <span v-else>➤</span>
      </button>
    </div>
    <div class="chat-input-hint">Enter 发送，Ctrl + Enter 也可发送</div>
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
  const value = message.value.trim()
  if (!value || props.loading) return
  emit('send', value)
  message.value = ''
}
</script>

<style scoped>
.chat-input-area {
  padding: 16px 20px;
  background: #ffffff;
  border-top: 1px solid #dbe3ef;
}

.chat-input-container {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  padding: 12px 16px;
  border: 1px solid #dbe3ef;
  border-radius: 16px;
  background: #f8fbff;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.chat-input-container:focus-within {
  border-color: #60a5fa;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
}

.chat-input {
  flex: 1;
  min-height: 24px;
  max-height: 160px;
  border: none;
  background: transparent;
  resize: none;
  outline: none;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  color: #0f172a;
}

.chat-input::placeholder {
  color: #94a3b8;
}

.chat-send-btn {
  width: 42px;
  height: 42px;
  border: none;
  border-radius: 50%;
  background: linear-gradient(135deg, #2563eb, #0ea5e9);
  color: #ffffff;
  cursor: pointer;
  font-size: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.2s ease, box-shadow 0.2s ease, opacity 0.2s ease;
}

.chat-send-btn:hover:not(:disabled) {
  transform: scale(1.05);
  box-shadow: 0 12px 24px rgba(37, 99, 235, 0.22);
}

.chat-send-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.chat-input-hint {
  margin-top: 8px;
  font-size: 12px;
  color: #94a3b8;
}
</style>
