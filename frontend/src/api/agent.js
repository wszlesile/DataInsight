import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000
})

export function invokeAgent(params) {
  return api.post('/agent/invoke', params)
}

export function listConversations(namespaceId) {
  return api.get('/insight/conversations', {
    params: {
      namespace_id: namespaceId
    }
  })
}

export function renameConversation(conversationId, title) {
  return api.put(`/insight/conversations/${conversationId}`, { title })
}

export function getConversationHistory(conversationId) {
  return api.get(`/insight/conversations/${conversationId}/history`)
}

export function getTurnDetail(conversationId, turnId) {
  return api.get(`/insight/conversations/${conversationId}/turns/${turnId}`)
}

export function listCollects(namespaceId) {
  return api.get('/insight/collects', {
    params: {
      namespace_id: namespaceId
    }
  })
}

export function createCollect(payload) {
  return api.post('/insight/collects', payload)
}

export function removeCollect(payload) {
  return api.delete('/insight/collects', { data: payload })
}

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
      let buffer = ''

      const read = () => {
        reader.read().then(({ done, value }) => {
          if (done) {
            if (buffer.trim()) {
              parseEvents(buffer, onMessage)
            }
            if (onDone) onDone()
            return
          }

          buffer += decoder.decode(value, { stream: true })
          const segments = buffer.split('\n\n')
          buffer = segments.pop() || ''
          segments.forEach(segment => parseEvents(segment, onMessage))
          read()
        })
      }

      read()
    })
    .catch(error => {
      if (error.name !== 'AbortError' && onError) {
        onError(error)
      }
    })

  return controller
}

function parseEvents(segment, onMessage) {
  const lines = segment.split('\n')
  lines.forEach(line => {
    if (!line.startsWith('data: ')) return
    const payload = line.slice(6).trim()
    if (!payload) return
    try {
      onMessage(JSON.parse(payload))
    } catch (error) {
      onMessage({
        type: 'message',
        level: 'warning',
        message: payload
      })
    }
  })
}

export default api
