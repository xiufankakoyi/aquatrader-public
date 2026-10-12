import { Server, StdioServerTransport } from "@modelcontextprotocol/sdk";
import { z } from "zod";
import { setTimeout as delay } from "node:timers/promises";

const BASE_URL = process.env.AQUATRADE_API ?? "http://127.0.0.1:5000";

const server = new Server(
  { name: "mcp-aquatrade", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.tool(
  "list_strategies",
  { description: "GET /strategies", inputSchema: z.object({}) },
  async () => {
    const r = await fetch(`${BASE_URL}/strategies`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }
);

server.tool(
  "run_backtest",
  {
    description: "POST /backtest -> {task_id}",
    inputSchema: z.object({
      strategy: z.string(),
      benchmark: z.string().default("000300"),
      start: z.string(),
      end: z.string(),
      params: z.record(z.any()).default({}),
      stream: z.boolean().default(false),
    }),
  },
  async (input) => {
    const r = await fetch(`${BASE_URL}/backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }
);

server.tool(
  "get_progress",
  {
    description: "GET /progress/:task_id",
    inputSchema: z.object({ task_id: z.string() }),
  },
  async ({ task_id }) => {
    const r = await fetch(`${BASE_URL}/progress/${task_id}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }
);

server.tool(
  "get_results",
  {
    description: "GET /results/:task_id",
    inputSchema: z.object({ task_id: z.string() }),
  },
  async ({ task_id }) => {
    const r = await fetch(`${BASE_URL}/results/${task_id}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  }
);

server.tool(
  "run_and_wait",
  {
    description: "启动回测并轮询至完成，最后返回结果",
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
  async ({ strategy, benchmark, start, end, params, poll_ms, timeout_ms }) => {
    const sr = await fetch(`${BASE_URL}/backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, benchmark, start, end, params, stream: false }),
    });
    if (!sr.ok) throw new Error(`HTTP ${sr.status}`);
    const { task_id } = await sr.json();

    const t0 = Date.now();
    while (true) {
      const pr = await fetch(`${BASE_URL}/progress/${task_id}`);
      if (!pr.ok) throw new Error(`HTTP ${pr.status}`);
      const js = await pr.json();
      if (js.progress >= 100) break;
      if (Date.now() - t0 > timeout_ms) throw new Error("Timeout waiting for backtest");
      await delay(poll_ms);
    }

    const rr = await fetch(`${BASE_URL}/results/${task_id}`);
    if (!rr.ok) throw new Error(`HTTP ${rr.status}`);
    return await rr.json();
  }
);

const transport = new StdioServerTransport();
server.connect(transport);
