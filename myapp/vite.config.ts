import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { dirname, resolve } from 'path'
import { fileURLToPath } from 'url'
import { createConnection } from 'net'

const configDir = dirname(fileURLToPath(import.meta.url))

function readPortEnv(): number | null {
  const raw = process.env.AQUATRADE_PROXY_PORT || process.env.VITE_PROXY_PORT
  if (!raw) return null

  const port = Number(raw)
  return Number.isInteger(port) && port > 0 ? port : null
}

function readPositiveIntEnv(name: string, fallback: number): number {
  const value = Number(process.env[name])
  return Number.isInteger(value) && value > 0 ? value : fallback
}

async function hasStockName(port: number, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const body = '{"page":1,"page_size":1}'
    const req = [
      'POST /api/screener/filter HTTP/1.1',
      'Host: 127.0.0.1',
      'Content-Type: application/json',
      `Content-Length: ${body.length}`,
      'Connection: close',
      '',
      body
    ].join('\r\n')

    const socket = createConnection({ port, host: '127.0.0.1', timeout: timeoutMs }, () => {
      socket.write(req, () => {})
    })

    let data = ''
    socket.setEncoding('utf8')
    socket.on('data', (chunk) => { data += chunk })
    socket.on('end', () => {
      socket.destroy()
      resolve(data.includes('"stock_name"'))
    })
    socket.on('error', () => resolve(false))
    socket.on('timeout', () => { socket.destroy(); resolve(false) })
  })
}

async function findAvailableProxyPort(startPort: number, maxRetries: number, timeoutMs: number): Promise<number> {
  for (let port = startPort; port < startPort + maxRetries; port++) {
    const ok = await hasStockName(port, timeoutMs)
    if (ok) {
      return port
    }
  }

  console.warn(`[proxy] backend probe failed for ports ${startPort}~${startPort + maxRetries - 1}; using ${startPort}. API requests may fail until backend is ready.`)
  return startPort
}

export default defineConfig(async () => {
  const configuredProxyPort = readPortEnv()
  const probeTimeoutMs = readPositiveIntEnv('AQUATRADE_PROXY_PROBE_TIMEOUT_MS', 1000)
  const maxRetries = readPositiveIntEnv('AQUATRADE_PROXY_PROBE_RETRIES', 3)
  let proxyPort: number

  if (configuredProxyPort) {
    proxyPort = configuredProxyPort
    const ok = await hasStockName(proxyPort, probeTimeoutMs)
    if (!ok) {
      console.warn(`[proxy] configured backend port ${proxyPort} is not ready; Vite will still start and proxy requests to it.`)
    }
  } else {
    proxyPort = await findAvailableProxyPort(5000, maxRetries, probeTimeoutMs)
  }

  console.log(`[proxy] backend target port: ${proxyPort}`)

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': resolve(configDir, 'src')
      }
    },
    server: {
      port: 5173,
      strictPort: true,
      host: true,
      proxy: {
        '/api': {
          target: `http://localhost:${proxyPort}`,
          changeOrigin: true,
          secure: false,
        },
        '/socket.io': {
          target: `http://localhost:${proxyPort}`,
          changeOrigin: true,
          ws: true,
          configure: (proxy, _options) => {
            proxy.on('error', (err, _req, _res) => {
              console.warn('[socket.io proxy]', err.message)
            })
          },
        }
      }
    },
    optimizeDeps: {
      include: [
        'vue',
        'vue-router',
        'pinia',
        'axios',
        'ant-design-vue',
        'monaco-editor'
      ],
      force: false
    },
    build: {
      target: 'esnext',
      minify: 'esbuild'
    }
  }
})
