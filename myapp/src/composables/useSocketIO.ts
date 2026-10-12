import { ref, onUnmounted, getCurrentInstance } from 'vue'
import { io, Socket } from 'socket.io-client'
import { useTimingLogger, formatDuration } from './useTimingLogger'

type MockSocket = {
  id: string
  connected: boolean
  emit: (eventName: string, data?: any) => void
  on: (eventName: string, callback: (data: any) => void) => void
  off: (eventName: string, callback: (data: any) => void) => void
}

// Socket.IO 实例 (我们使用单例模式，让整个应用共享一个连接)
let socket: Socket | null = null
const status = ref('CLOSED') // 'CLOSED', 'CONNECTING', 'OPEN', 'ERROR'
const connectionUrl = ref('')

// 重连配置
const RECONNECT_ATTEMPTS = 5
const RECONNECT_DELAY = 2000
let reconnectTimer: NodeJS.Timeout | null = null
let reconnectAttempt = 0

// 等待连接的队列
interface PendingEvent {
  eventName: string
  data: any
}
const pendingEvents: PendingEvent[] = []

// Mock Socket 实例
let mockSocketInstance: MockSocket | null = null
let mockSocketModule: Promise<typeof import('../api/mockSocketIO')> | null = null

function loadMockSocketModule() {
  mockSocketModule ??= import('../api/mockSocketIO')
  return mockSocketModule
}

function ensureMockSocket(): Promise<MockSocket> {
  return loadMockSocketModule().then(({ getMockSocket }) => {
    mockSocketInstance ??= getMockSocket()
    return mockSocketInstance
  })
}

export function useSocketIO() {
  const { start: startTiming, end: endTiming } = useTimingLogger()

  // 检查是否在 Mock 模式
  const isMockMode = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === 'true'

  // --- 核心功能 ---

  /**
   * 连接到 Socket.IO 服务器
   * @param url 服务器地址 (e.g., 'http://localhost:5000')
   */
  const connect = (url: string) => {
    // Mock 模式下使用 Mock Socket
    if (isMockMode) {
      console.log('[Socket.IO] Mock 模式：使用 Mock Socket')
      void ensureMockSocket().then(() => {
        status.value = 'OPEN'
      })
      return
    }

    // 如果 URL 改变，需要断开旧连接并重新连接
    if (connectionUrl.value && connectionUrl.value !== url) {
      console.log(`Socket.IO URL 改变: ${connectionUrl.value} -> ${url}，重新连接`)
      disconnect()
    }

    if (socket && (socket.connected || status.value === 'CONNECTING')) {
      console.log('Socket.IO 已经连接或正在连接中')
      return // 已经连接或正在连接，无需重复操作
    }

    connectionUrl.value = url
    reconnectAttempt = 0

    startTiming('socket_connect', { url })

    try {
      // 配置选项
      const options = {
        path: '/socket.io',  // 明确指定 Socket.IO 路径
        reconnection: true,
        reconnectionAttempts: RECONNECT_ATTEMPTS,
        reconnectionDelay: RECONNECT_DELAY,
        reconnectionDelayMax: 5000,
        timeout: 20000, // 增加连接超时时间
        // ASGI 模式（Granian + python-socketio AsyncServer）
        // Vite 代理 WebSocket 存在兼容性问题，暂时只用 polling
        transports: ['polling'],
        withCredentials: false, // 不使用凭证
        autoConnect: true, // 自动连接
        forceNew: true, // 强制使用新连接
      }

      socket = io(url, options)
      status.value = 'CONNECTING'

      // --- 监听内置事件 ---
      socket.on('connect', () => {
        const duration = endTiming('socket_connect')
        console.log(`Socket.IO 已连接: ${socket?.id || '未知ID'} (${duration ? formatDuration(duration) : '未知时间'})`)
        status.value = 'OPEN'
        reconnectAttempt = 0

        // 处理连接成功后的待发送事件
        if (pendingEvents.length > 0) {
          console.log(`处理 ${pendingEvents.length} 个待发送事件`)
          while (pendingEvents.length > 0) {
            const event = pendingEvents.shift()
            if (event && socket?.connected) {
              socket.emit(event.eventName, event.data)
            }
          }
        }
      })

      socket.on('disconnect', (reason) => {
        console.log('Socket.IO 已断开:', reason)
        status.value = 'CLOSED'

        // 如果是异常断开，尝试重连
        if (reason !== 'io client disconnect') {
          attemptReconnect()
        } else {
          socket = null // 主动断开时清理实例
        }
      })

      socket.on('connect_error', (err) => {
        // Mock 模式下不显示连接错误
        if (isMockMode) return

        console.error('Socket.IO 连接错误:', err.message || err)
        status.value = 'ERROR'

        // 添加更详细的错误日志
        if (err.description) {
          console.error('错误描述:', err.description)
        }
        if (err.context) {
          console.error('错误上下文:', err.context)
        }

        // 增加指数退避的重连策略
        attemptReconnect()
      })

      // 添加对错误事件的监听
      socket.on('error', (err) => {
        // Mock 模式下不显示错误
        if (isMockMode) return
        console.error('Socket.IO 发生错误:', err)
      })

      // 添加对重连失败的监听
      socket.on('reconnect_failed', () => {
        // Mock 模式下不显示重连失败
        if (isMockMode) return
        console.error('Socket.IO 重连失败，停止重试')
        status.value = 'ERROR'
      })
    } catch (error) {
      console.error('创建 Socket.IO 实例失败:', error)
      status.value = 'ERROR'
    }
  }

  /**
   * 尝试重新连接
   */
  const attemptReconnect = () => {
    // Mock 模式下不重连
    if (isMockMode) return

    if (reconnectAttempt >= RECONNECT_ATTEMPTS) {
      console.error(`已达到最大重连次数 (${RECONNECT_ATTEMPTS})，停止重连`)
      return
    }

    reconnectAttempt++
    console.log(`尝试第 ${reconnectAttempt} 次重连...`)

    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
    }

    reconnectTimer = setTimeout(() => {
      if (connectionUrl.value) {
        console.log(`正在重连到 ${connectionUrl.value}...`)
        connect(connectionUrl.value)
      }
    }, RECONNECT_DELAY * reconnectAttempt) // 指数退避
  }

  /**
   * 断开 Socket.IO 连接
   */
  const disconnect = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    
    if (socket) {
      socket.disconnect()
      socket = null
      status.value = 'CLOSED'
    }
    
    // 清空待处理事件
    pendingEvents.length = 0
  }

  /**
   * 向服务器发送一个事件，如果连接未建立则加入队列
   * @param eventName 事件名称 (e.g., 'run_streaming_backtest')
   * @param data 要发送的数据 (JSON 对象)
   */
  const emitEvent = (eventName: string, data: any) => {
    startTiming(`emit_${eventName}`, { eventName, data })

    // Mock 模式下使用 Mock Socket
    if (isMockMode) {
      void ensureMockSocket().then((mockSocket) => {
        mockSocket.emit(eventName, data)
        endTiming(`emit_${eventName}`)
      })
      return
    }

    if (socket && socket.connected) {
      socket.emit(eventName, data)
      const duration = endTiming(`emit_${eventName}`)
      console.log(`已发送事件 '${eventName}' (${duration ? formatDuration(duration) : '未知时间'})`, data)
    } else {
      // 如果没有连接或连接未建立，将事件加入队列
      console.warn(`Socket 未连接，将事件 '${eventName}' 加入待发送队列`)
      pendingEvents.push({ eventName, data })

      // 如果尚未连接，尝试连接
      if (connectionUrl.value && (!socket || !socket.connected)) {
        connect(connectionUrl.value)
      }
    }
  }

  /**
   * 监听来自服务器的事件
   * @param eventName 事件名称 (e.g., 'optimization_progress')
   * @param callback 收到事件时的回调函数
   */
  const onEvent = (eventName: string, callback: (data: any) => void) => {
    // Mock 模式下使用 Mock Socket
    if (isMockMode) {
      // 包装回调函数以添加计时
      const timedCallback = (data: any) => {
        startTiming(`on_${eventName}`, { eventName })
        try {
          callback(data)
          const duration = endTiming(`on_${eventName}`)
          if (duration && duration > 100) {
            console.log(`[⏱️ Mock事件处理] ${eventName}: ${formatDuration(duration)}`)
          }
        } catch (error) {
          endTiming(`on_${eventName}`)
          console.error(`[❌ Mock事件处理错误] ${eventName}:`, error)
          throw error
        }
      }

      let active = true
      void ensureMockSocket().then((mockSocket) => {
        if (active) mockSocket.on(eventName, timedCallback)
      })

      // 返回一个 "取消监听" 的函数
      return () => {
        active = false
        mockSocketInstance?.off(eventName, timedCallback)
      }
    }

    if (!socket) {
      console.warn(`Socket 未初始化，无法监听事件 '${eventName}'`)
      // 返回一个空函数，防止在 onUnmounted 中调用时出错
      return () => {}
    }

    // 包装回调函数以添加计时
    const timedCallback = (data: any) => {
      console.log(`[SocketIO] 收到事件 '${eventName}', 数据:`, data);
      startTiming(`on_${eventName}`, { eventName })
      try {
        callback(data)
        const duration = endTiming(`on_${eventName}`)
        if (duration && duration > 100) { // 只记录耗时较长的事件
          console.log(`[⏱️ 事件处理] ${eventName}: ${formatDuration(duration)}`)
        }
      } catch (error) {
        endTiming(`on_${eventName}`)
        console.error(`[❌ 事件处理错误] ${eventName}:`, error)
        throw error
      }
    }

    // 注册监听
    socket.on(eventName, timedCallback)

    // 返回一个 "取消监听" 的函数
    return () => {
      socket?.off(eventName, timedCallback)
    }
  }

  const getId = () => {
    if (isMockMode && mockSocketInstance) {
      return mockSocketInstance.id
    }
    return socket?.id ?? null
  }

  // 清理资源
  // CHANGED: 只在组件上下文中注册 onUnmounted
  const instance = getCurrentInstance()
  if (instance) {
    onUnmounted(() => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer)
      }
      // 不再在组件卸载时主动断开全局 Socket，避免路由切换导致全局连接被关闭
    })
  }
  // 如果不在组件上下文中（如在 composable 或异步函数中），不注册清理函数
  // 这种情况下，清理会在应用关闭时自动进行

  return {
    status,     // 返回响应式的连接状态
    connect,    // 连接方法
    disconnect, // 断开方法
    emitEvent,  // 发送事件方法
    onEvent,    // 监听事件方法
    getId,      // 获取当前 socket id
    isConnected: () => {
      if (isMockMode && mockSocketInstance) {
        return mockSocketInstance.connected
      }
      return socket?.connected || false
    } // 连接状态检查函数
  }
}
