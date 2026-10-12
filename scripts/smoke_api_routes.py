#!/usr/bin/env python
"""Smoke the running AquaTrade HTTP, Socket.IO and frontend surfaces."""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--no-frontend", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base = args.base_url.rstrip("/")
    checks = [
        ("research_health", f"{base}/api/research/health", "json"),
        ("quant_health", f"{base}/api/quant/health", "json"),
        ("theme_runs", f"{base}/api/quant/themes/runs", "json"),
        ("data_health", f"{base}/api/data/health", "json"),
        ("portfolio", f"{base}/api/portfolio/positions", "json"),
        ("similarity", f"{base}/api/similarity/status", "json"),
        ("optimization", f"{base}/api/optimization/health", "json"),
        ("socketio", f"{base}/socket.io/?EIO=4&transport=polling", "socket"),
    ]
    if not args.no_frontend:
        frontend = args.frontend_url.rstrip("/")
        checks.extend([
            ("dashboard", f"{frontend}/dashboard", "html"),
            ("knowledge_graph", f"{frontend}/knowledge-graph", "html"),
        ])

    failures: list[dict[str, object]] = []
    for name, url, expected in checks:
        started = time.time()
        try:
            response = requests.get(url, timeout=max(1.0, args.timeout))
            valid_body = True
            if expected == "json":
                response.json()
            elif expected == "socket":
                valid_body = response.text.startswith("0{")
            elif expected == "html":
                valid_body = "<html" in response.text.lower()
            ok = response.status_code == 200 and valid_body
            row = {
                "name": name,
                "ok": ok,
                "status": response.status_code,
                "elapsed_seconds": round(time.time() - started, 3),
            }
            print(json.dumps(row, ensure_ascii=False))
            if not ok:
                failures.append(row)
        except Exception as exc:
            row = {"name": name, "ok": False, "error": str(exc)}
            print(json.dumps(row, ensure_ascii=False))
            failures.append(row)
    print(json.dumps({"passed": len(checks) - len(failures), "failed": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
