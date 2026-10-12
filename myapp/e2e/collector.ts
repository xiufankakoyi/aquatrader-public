/**
 * 浏览器运行时信号采集器
 *
 * 职责:
 *  - 监听 page.on('console') / 'pageerror' / 'response', 聚合为结构化事件
 *  - 区分 4xx/5xx API 响应, 仅记录 /api/* 路径, 排除静态资源与 ws 握手
 *  - 捕获 JSON.parse 失败: 监听 response, 当 Content-Type 含 json 但 body 解析失败时上报
 *
 * 设计:
 *  - 不在采集器内做断言, 只暴露原始事件列表, 由 spec 决定容忍策略
 *  - 事件按出现顺序追加, 便于失败时按时间序打印
 *  - 采集器只在测试用例作用域内有效, 用完即随 page 释放
 */
import type { Page, Response } from '@playwright/test';

/** 控制台事件 */
export interface ConsoleEvt {
  kind: 'console';
  type: string; // log / error / warning / ...
  text: string;
  location?: string;
}

/** 未捕获的 page error */
export interface PageErrorEvt {
  kind: 'pageerror';
  message: string;
  stack?: string;
}

/** HTTP 响应事件 (按状态码分桶) */
export interface ResponseEvt {
  kind: 'response';
  url: string;
  status: number;
  method: string;
  ok: boolean;
  contentType: string;
  jsonParseFailed?: boolean;
}

/** JSON 解析失败事件 (从 response 衍生) */
export interface JsonParseEvt {
  kind: 'json_parse_failed';
  url: string;
  status: number;
  preview: string; // body 前 200 字符, 便于排障
}

export type RuntimeEvent = ConsoleEvt | PageErrorEvt | ResponseEvt | JsonParseEvt;

/**
 * 运行时信号采集器
 *
 * 暴露 events / errors / pageErrors / badResponses / jsonParseFailures 五类视图,
 * spec 可按需取用。
 */
export class RuntimeCollector {
  readonly events: RuntimeEvent[] = [];
  private readonly page: Page;

  constructor(page: Page) {
    this.page = page;
  }

  /** 挂载所有监听器 (顺序: console -> pageerror -> response) */
  attach(): void {
    this.page.on('console', (msg) => {
      // 仅采集 error/warning, 过滤掉 vite/loader 的预期 info 日志
      const t = msg.type();
      if (t !== 'error' && t !== 'warning') return;
      const loc = msg.location();
      this.events.push({
        kind: 'console',
        type: t,
        text: msg.text(),
        location: loc ? `${loc.url}:${loc.lineNumber}` : undefined,
      });
    });

    this.page.on('pageerror', (err) => {
      this.events.push({
        kind: 'pageerror',
        message: err.message,
        stack: err.stack,
      });
    });

    this.page.on('response', async (resp) => {
      const url = resp.url();
      const status = resp.status();
      const method = resp.request().method();
      const ok = resp.ok();
      const contentType = (resp.headers()['content-type'] || '').toLowerCase();

      const evt: ResponseEvt = {
        kind: 'response',
        url,
        status,
        method,
        ok,
        contentType,
      };
      this.events.push(evt);

      // JSON 解析失败: 仅对 2xx + application/json 检查, 避免对错误响应做无意义重试
      if (ok && contentType.includes('json')) {
        try {
          await resp.json();
        } catch (e) {
          evt.jsonParseFailed = true;
          let preview = '';
          try {
            preview = (await resp.text()).slice(0, 200);
          } catch {
            preview = `<read body failed: ${(e as Error).message}>`;
          }
          this.events.push({
            kind: 'json_parse_failed',
            url,
            status,
            preview,
          });
        }
      }
    });
  }

  /**
   * 手动追加一条 console error 事件
   *
   * 用例在自身流程 (例如: 首屏响应非 200) 中若想把它计入"console.error"集合,
   * 可以通过此方法追加, 避免直接修改 getter 返回的只读数组。
   */
  addConsoleError(text: string, location?: string): void {
    this.events.push({
      kind: 'console',
      type: 'error',
      text,
      location,
    });
  }

  /** console.error 集合 (Vue 组件内部 throw 出来的也走这里) */
  get consoleErrors(): ConsoleEvt[] {
    return this.events.filter(
      (e): e is ConsoleEvt => e.kind === 'console' && e.type === 'error'
    );
  }

  /** 未捕获 pageerror 集合 */
  get pageErrors(): PageErrorEvt[] {
    return this.events.filter((e): e is PageErrorEvt => e.kind === 'pageerror');
  }

  /** 4xx/5xx 响应集合 (排除静态资源 & socket.io 握手) */
  get badResponses(): ResponseEvt[] {
    return this.events.filter(
      (e): e is ResponseEvt =>
        e.kind === 'response' && e.status >= 400 && isApiLike(e.url)
    );
  }

  /** 5xx 响应集合, 用于触发"必须有 ErrorState"硬性断言 */
  get serverErrors(): ResponseEvt[] {
    return this.badResponses.filter((e) => e.status >= 500);
  }

  /** 4xx 响应集合, 用于触发"必须被 ErrorState/TimeoutState/ApiDebugPanel 展示"软性断言 */
  get clientErrors(): ResponseEvt[] {
    return this.badResponses.filter((e) => e.status >= 400 && e.status < 500);
  }

  /** JSON 解析失败集合 */
  get jsonParseFailures(): JsonParseEvt[] {
    return this.events.filter((e): e is JsonParseEvt => e.kind === 'json_parse_failed');
  }
}

/** 判断 URL 是否属于"业务 API 域", 排除静态资源与 ws 握手 */
function isApiLike(url: string): boolean {
  if (url.includes('/api/')) return true;
  if (url.includes('/socket.io/')) return false;
  // 其他同源路径默认不是 API, 避免把 404 静态资源计入
  return false;
}

/**
 * 等待网络空闲或短时兜底 (封装 waitForLoadState, 避免每个用例都写一遍)
 *
 * 策略: networkidle 优先; 长轮询/SSE 页面在 8s 后继续做运行时断言。
 */
export async function waitForNetworkIdle(page: Page, timeoutMs = 8_000): Promise<void> {
  try {
    await page.waitForLoadState('networkidle', { timeout: timeoutMs });
  } catch {
    // networkidle 偶发在 long-poll / SSE 下不会触发, 这里静默
    // 后续断言会基于已采集的事件继续, 不抛错
  }
}
