import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

import { z } from "zod";

// Node20 自带 fetch；若旧版 Node，可改用 undici fetch
const BASE_URL = process.env.AQUATRADE_API ?? "http://127.0.0.1:5000";

const server = new McpServer(
  {
    name: "mcp-aquatrade",
    version: "0.1.0",
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// 1) 列出策略
server.registerTool(
  "list_strategies",
  {
    description: "获取可用策略列表（后端 /api/strategies）",
    inputSchema: z.object({}),
  },
  async () => {
    const res = await fetch(`${BASE_URL}/api/strategies`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    // MCP SDK 期望返回包含 content 的对象
    const strategies = data.success ? data.data : data;
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(strategies, null, 2)
        }
      ]
    };
  }
);

// 2) 触发回测（使用现有的 /api/run_backtest 端点）
server.registerTool(
  "run_backtest",
  {
    description: "启动回测并返回结果（使用 /api/run_backtest）",
    inputSchema: z.object({
      strategy: z.string(),
      benchmark: z.string().optional(),
      start: z.string(), // YYYY-MM-DD
      end: z.string(),
      params: z.record(z.any()).default({}),
    }),
  },
  async ({ strategy, benchmark, start, end }) => {
    const res = await fetch(`${BASE_URL}/api/run_backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ 
        strategy_name: strategy,
        start_date: start,
        end_date: end,
        benchmark_code: benchmark
      }),
    });
    if (!res.ok) {
      const errorText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errorText}`);
    }
    const data = await res.json();
    // MCP SDK 期望返回包含 content 的对象
    const result = data.success ? data.data : data;
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(result, null, 2)
        }
      ]
    };
  }
);

// 3) 轮询进度（如果你有 /progress/<task_id>）
server.registerTool(
  "get_progress",
  {
    description: "查询回测进度（0~100）",
    inputSchema: z.object({ task_id: z.string() }),
  },
  async ({ task_id }) => {
    const res = await fetch(`${BASE_URL}/progress/${task_id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(data, null, 2)
        }
      ]
    };
  }
);

// 4) 拉取结果（如指标、权益曲线、每日收益）
server.registerTool(
  "get_results",
  {
    description: "获取回测结果（指标、资金曲线、交易记录等）",
    inputSchema: z.object({ task_id: z.string() }),
  },
  async ({ task_id }) => {
    const res = await fetch(`${BASE_URL}/results/${task_id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(data, null, 2)
        }
      ]
    };
  }
);

// 5) 一个"阻塞式等待完成"的工具（直接调用 run_backtest，因为它是同步的）
server.registerTool(
  "run_and_wait",
  {
    description: "启动回测并等待完成后返回结果（使用 /api/run_backtest）",
    inputSchema: z.object({
      strategy: z.string(),
      benchmark: z.string().default("000300"),
      start: z.string(),
      end: z.string(),
      params: z.record(z.any()).default({}),
      poll_ms: z.number().default(800),
      timeout_ms: z.number().default(30 * 60 * 1000),
    }),
  },
  async ({ strategy, benchmark, start, end }) => {
    // 直接调用 run_backtest，因为 /api/run_backtest 是同步的
    const res = await fetch(`${BASE_URL}/api/run_backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ 
        strategy_name: strategy,
        start_date: start,
        end_date: end,
        benchmark_code: benchmark
      }),
    });
    if (!res.ok) {
      const errorText = await res.text();
      throw new Error(`HTTP ${res.status}: ${errorText}`);
    }
    const data = await res.json();
    // 返回格式：{ success: true, data: {...} } 或直接返回数据
    return data.success ? data.data : data;
  }
);

const transport = new StdioServerTransport();
server.connect(transport);
