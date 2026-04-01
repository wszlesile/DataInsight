import axios from 'axios'

const AUTHORIZATION_HEADER = 'Bearer test'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000
})

api.interceptors.request.use((config) => {
  config.headers = config.headers || {}
  config.headers.Authorization = AUTHORIZATION_HEADER
  return config
})

export function invokeAgent(params) {
  return api.post('/agent/invoke', params)
}

export function streamAgent(params, onMessage, onError, onDone) {
  const controller = new AbortController()

  fetch('/api/agent/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: AUTHORIZATION_HEADER
    },
    body: JSON.stringify(params),
    signal: controller.signal
  })
    .then(response => {
      if (!response.ok || !response.body) {
        throw new Error(`Stream request failed: ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      const emitEvent = (rawEvent) => {
        const dataLines = rawEvent
          .split('\n')
          .filter(line => line.startsWith('data: '))
          .map(line => line.slice(6))

        if (!dataLines.length) return

        const payload = dataLines.join('\n')
        try {
          onMessage(JSON.parse(payload))
        } catch {
          onMessage({ type: 'message', message: payload })
        }
      }

      const read = () => {
        reader.read().then(({ done, value }) => {
          if (done) {
            if (buffer.trim()) {
              emitEvent(buffer.trim())
            }
            if (onDone) onDone()
            return
          }

          buffer += decoder.decode(value, { stream: true })
          const events = buffer.split('\n\n')
          buffer = events.pop() || ''

          for (const rawEvent of events) {
            if (rawEvent.trim()) {
              emitEvent(rawEvent.trim())
            }
          }

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

export default api
