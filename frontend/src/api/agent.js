import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000
})

/**
 * 同步调用Agent
 * @param {Object} params - 请求参数
 * @param {string} params.username - 用户名
 * @param {string} params.namespace_id - 命名空间ID
 * @param {string} params.conversation_id - 会话ID
 * @param {string} params.user_message - 用户消息
 */
export function invokeAgent(params) {
  return api.post('/agent/invoke', params)
}

/**
 * 流式调用Agent (SSE) - 使用fetch实现
 * @param {Object} params - 请求参数
 * @param {Function} onMessage - 消息回调 (streamMode, chunk)
 * @param {Function} onError - 错误回调
 * @param {Function} onDone - 完成回调
 * @returns {AbortController} 可用于取消请求
 */
export function streamAgent(params, onMessage, onError, onDone) {
  const controller = new AbortController()

  fetch('/api/agent/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(params),
    signal: controller.signal
  })
    .then(response => {
      const reader = response.body.getReader()
      const decoder = new TextDecoder()

      const read = () => {
        reader.read().then(({ done, value }) => {
          if (done) {
            if (onDone) onDone()
            return
          }

          const chunk = decoder.decode(value, { stream: true })
          // 解析 SSE 格式: data: stream_mode:content\n\n
          const lines = chunk.split('\n')
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              const content = line.slice(6)
              const colonIndex = content.indexOf(':')
              if (colonIndex > 0) {
                const streamMode = content.slice(0, colonIndex)
                const data = content.slice(colonIndex + 1)
                onMessage(streamMode, data)
              } else {
                onMessage('message', content)
              }
            }
          }

          read()
        })
      }

      read()
    })
    .catch(error => {
      if (error.name !== 'AbortError') {
        if (onError) onError(error)
      }
    })

  return controller
}

export default api