import axios from 'axios'

function getAuthorizationHeader() {
  const rawTicket = window.localStorage.getItem('ticket') || ''
  const ticket = rawTicket.trim()
  if (!ticket) return ''
  return ticket.startsWith('Bearer ') ? ticket : `Bearer ${ticket}`
}

const api = axios.create({
  baseURL: '/api',
  timeout: 120000
})

api.interceptors.request.use((config) => {
  const authorization = getAuthorizationHeader()
  if (authorization) {
    config.headers = config.headers || {}
    config.headers.Authorization = authorization
  }
  return config
})

export function invokeAgent(params) {
  return api.post('/agent/invoke', params)
}

export function listNamespaces() {
  return api.get('/insight/namespaces')
}

export function createNamespace(name) {
  return api.post('/insight/namespaces', { name })
}

export function renameNamespace(namespaceId, name) {
  return api.put(`/insight/namespaces/${namespaceId}`, { name })
}

export function deleteNamespace(namespaceId) {
  return api.delete(`/insight/namespaces/${namespaceId}`)
}

export function listConversations(namespaceId) {
  return api.get('/insight/conversations', {
    params: {
      namespace_id: namespaceId
    }
  })
}

export function createConversation(namespaceId, title = '') {
  return api.post('/insight/conversations', {
    namespace_id: namespaceId,
    title
  })
}

export function renameConversation(conversationId, title) {
  return api.put(`/insight/conversations/${conversationId}`, { title })
}

export function deleteConversation(conversationId) {
  return api.delete(`/insight/conversations/${conversationId}`)
}

export function getConversationHistory(conversationId) {
  return api.get(`/insight/conversations/${conversationId}/history`)
}

export function getTurnDetail(conversationId, turnId) {
  return api.get(`/insight/conversations/${conversationId}/turns/${turnId}`)
}

export function bindConversationDatasource(conversationId, datasourceId) {
  return api.post('/insight/conversation/datasource/', {
    insight_conversation_id: conversationId,
    datasource_id: datasourceId
  })
}

export function unbindConversationDatasource(conversationId, datasourceId) {
  return api.delete('/insight/conversation/datasource/', {
    data: {
      insight_conversation_id: conversationId,
      datasource_id: datasourceId
    }
  })
}

export function listNamespaceDatasources(namespaceId, conversationId) {
  return api.get(`/insight/namespaces/${namespaceId}/datasources`, {
    params: conversationId
      ? {
          insight_conversation_id: conversationId
        }
      : {}
  })
}

export function uploadNamespaceDatasource(namespaceId, file) {
  const formData = new FormData()
  formData.append('file', file)
  return api.post(`/insight/namespaces/${namespaceId}/datasources/upload`, formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

export function importNamespaceUnsDatasources(namespaceId, conversationId, nodes) {
  return api.post(`/insight/namespaces/${namespaceId}/datasources/import-uns`, {
    insight_conversation_id: conversationId,
    nodes
  })
}

export function listNamespaceUnsSelections(namespaceId, conversationId) {
  return api.get(`/insight/namespaces/${namespaceId}/uns/selections`, {
    params: {
      insight_conversation_id: conversationId
    }
  })
}

export function deleteNamespaceDatasource(namespaceId, datasourceId) {
  return api.delete(`/insight/namespaces/${namespaceId}/datasources/${datasourceId}`)
}

export function updateNamespaceDatasourceDescription(namespaceId, datasourceId, description) {
  return api.put(`/insight/namespaces/${namespaceId}/datasources/${datasourceId}/description`, {
    description
  })
}

export function fetchUnsTreeNodes(namespaceId, parentId = '0', options = {}) {
  return api.post(
    `/insight/namespaces/${namespaceId}/uns/tree`,
    {
      parentId,
      pageNo: 1,
      pageSize: 100,
      keyword: options.keyword || '',
      searchType: 1
    }
  )
}

export function exportTurnPdf(conversationId, turnId, payload) {
  return api.post(
    `/insight/conversations/${conversationId}/turns/${turnId}/export/pdf`,
    payload || {},
    { responseType: 'blob' }
  )
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
  return streamRequest('/api/agent/stream', params, onMessage, onError, onDone)
}

export function streamRerunTurn(conversationId, turnId, onMessage, onError, onDone) {
  return streamRequest(
    `/api/insight/conversations/${conversationId}/turns/${turnId}/rerun/stream`,
    {},
    onMessage,
    onError,
    onDone
  )
}

function streamRequest(url, params, onMessage, onError, onDone) {
  const controller = new AbortController()
  const headers = {
    'Content-Type': 'application/json'
  }
  const authorization = getAuthorizationHeader()
  if (authorization) {
    headers.Authorization = authorization
  }

  fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(params || {}),
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
