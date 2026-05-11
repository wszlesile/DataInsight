import axios from 'axios'

function getAuthorizationHeader() {
  const rawTicket = window.localStorage.getItem('ticket') || ''
  const ticket = rawTicket.trim()
  if (!ticket) return ''
  return ticket.startsWith('Bearer ') ? ticket : `Bearer ${ticket}`
}

function getAppBasePath() {
  const pathname = window.location.pathname || '/'
  const platformMatch = pathname.match(/^\/os\/[^/]+\/[^/]+(?:\/|$)/)
  if (platformMatch) {
    return platformMatch[0].endsWith('/') ? platformMatch[0] : `${platformMatch[0]}/`
  }
  return '/'
}

function joinAppPath(path) {
  const appBasePath = getAppBasePath().replace(/\/$/, '')
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${appBasePath}${normalizedPath}`
}

const API_BASE_PATH = joinAppPath('/api')

const api = axios.create({
  baseURL: API_BASE_PATH,
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

export function submitAgentTask(params) {
  return api.post('/agent/tasks', params)
}

export function getAnalysisTask(taskId) {
  return api.get(`/agent/tasks/${taskId}`)
}

export function listLlmModels() {
  return api.get('/llm/models')
}

export function selectLlmModel(modelId) {
  return api.put('/llm/models/selected', { model_id: modelId })
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

export function getRunningTurn(conversationId) {
  return api.get(`/insight/conversations/${conversationId}/running-turn`)
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

export function listCollects() {
  return api.get('/insight/collects')
}

export function createCollect(payload) {
  return api.post('/insight/collects', payload)
}

export function removeCollect(payload) {
  return api.delete('/insight/collects', { data: payload })
}

export function streamAgent(params, onMessage, onError, onDone) {
  return streamRequest(`${API_BASE_PATH}/agent/stream`, params, onMessage, onError, onDone)
}

export function streamTurnEvents(conversationId, turnId, onMessage, onError, onDone) {
  return streamGetRequest(
    `${API_BASE_PATH}/insight/conversations/${conversationId}/turns/${turnId}/stream`,
    onMessage,
    onError,
    onDone
  )
}

export function submitRerunTurnTask(conversationId, turnId) {
  return api.post(`/insight/conversations/${conversationId}/turns/${turnId}/rerun/task`, {})
}

export function streamRerunTurn(conversationId, turnId, onMessage, onError, onDone) {
  return streamRequest(
    `${API_BASE_PATH}/insight/conversations/${conversationId}/turns/${turnId}/rerun/stream`,
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
    .then(async response => {
      if (!response.ok) {
        throw new Error(await extractStreamErrorMessage(response))
      }
      if (!response.body) {
        throw new Error('流式响应为空，无法获取实时分析结果。')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
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
      }
    })
    .catch(error => {
      if (error.name !== 'AbortError' && onError) {
        onError(error)
      }
    })

  return controller
}

function streamGetRequest(url, onMessage, onError, onDone) {
  const controller = new AbortController()
  const headers = {}
  const authorization = getAuthorizationHeader()
  if (authorization) {
    headers.Authorization = authorization
  }

  fetch(url, {
    method: 'GET',
    headers,
    signal: controller.signal
  })
    .then(async response => {
      if (!response.ok) {
        throw new Error(await extractStreamErrorMessage(response))
      }
      if (!response.body) {
        throw new Error('流式响应为空，无法获取实时分析结果。')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
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
      }
    })
    .catch(error => {
      if (error.name !== 'AbortError' && onError) {
        onError(error)
      }
    })

  return controller
}

async function extractStreamErrorMessage(response) {
  const fallback = `请求失败（HTTP ${response.status}）`
  try {
    const contentType = response.headers.get('Content-Type') || ''
    if (contentType.includes('application/json')) {
      const data = await response.json()
      return data?.message || data?.error || fallback
    }
    const text = (await response.text()).trim()
    return text || fallback
  } catch (error) {
    return fallback
  }
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
