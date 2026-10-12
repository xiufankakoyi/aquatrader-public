"""Autonomous Perilla Discovery Pipeline -- CLI entry point.

Orchestrates the Perilla Discovery Engine from the command line,
parsing arguments, creating the engine, and printing a summary report.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict

# Project root
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

from core.research.perilla.discovery.autonomous_perilla_engine import AutonomousPerillaEngine
from core.research.perilla.discovery.models import PerillaDiscoveryReport

logger = logging.getLogger(__name__)


def run_pipeline(argv: list[str] | None = None) -> Dict[str, Any]:
    """Run the Autonomous Perilla Discovery Pipeline.

    Parses CLI arguments, creates an AutonomousPerillaEngine instance,
    executes the discovery run, and prints a summary.

    Args:
        argv: Optional argument list (for testing). None means sys.argv.

    Returns:
        Pipeline result dictionary with summary and report data.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Validate: topic mode requires --topic
    if args.mode == "topic" and not args.topic:
        parser.error("--topic is required when --mode is 'topic'")

    args.topic = args.topic.strip() if isinstance(args.topic, str) else None
    if args.mode == "topic" and not args.topic:
        parser.error("--topic is required when --mode is 'topic'")
    args.lookback_days = max(0, args.lookback_days)
    args.max_concepts = max(0, args.max_concepts)
    args.max_sources_per_concept = max(0, args.max_sources_per_concept)
    args.max_candidates = max(0, args.max_candidates)

    t0 = time.time()
    report_meta: Dict[str, Any] = {
        "pipeline": "autonomous_perilla_discovery",
        "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": args.mode,
        "topic": args.topic,
        "lookback_days": args.lookback_days,
        "max_concepts": args.max_concepts,
        "max_sources_per_concept": args.max_sources_per_concept,
        "max_candidates": args.max_candidates,
    }

    # Build engine kwargs from CLI args
    engine_kwargs = _args_to_engine_kwargs(args)

    try:
        engine = AutonomousPerillaEngine(**engine_kwargs)
        report: PerillaDiscoveryReport = engine.run()
    except Exception as exc:
        logger.exception("AutonomousPerillaEngine execution failed")
        report_meta["success"] = False
        report_meta["error"] = str(exc)
        report_meta["elapsed_ms"] = round((time.time() - t0) * 1000)
        _print_summary(report_meta)
        return report_meta

    elapsed_ms = round((time.time() - t0) * 1000)

    # Build summary
    summary = {
        **report_meta,
        "success": True,
        "concepts_found": len(report.concept_signals),
        "candidates_found": len(report.company_candidates),
        "bottlenecks_found": len(report.bottleneck_hypotheses),
        "review_items_count": len(report.review_queue),
        "discovery_items_count": len(report.discovery_items),
        "elapsed_ms": elapsed_ms,
        "completed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Write report to output directory
    _write_report(report, args.output_dir)

    _print_summary(summary)
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with all Perilla Discovery CLI arguments.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Autonomous Perilla Discovery Pipeline v1.25",
    )

    parser.add_argument(
        "--mode",
        choices=["auto", "topic"],
        default="topic",
        help="Discovery mode: auto (multi-source scan) or topic (single topic deep-dive). Default: topic",
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Topic name (required when mode=topic)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Lookback window in days for concept discovery. Default: 30",
    )
    parser.add_argument(
        "--max-concepts",
        type=int,
        default=5,
        help="Maximum number of concepts to process. Default: 5",
    )
    parser.add_argument(
        "--max-sources-per-concept",
        type=int,
        default=10,
        help="Maximum source documents per concept. Default: 10",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=20,
        help="Maximum company candidates to output. Default: 20",
    )

    # Feature flags
    parser.add_argument(
        "--enable-search",
        action="store_true",
        default=False,
        help="Enable SearXNG search discovery",
    )
    parser.add_argument(
        "--enable-rss",
        action="store_true",
        default=False,
        help="Enable RSS feed collection",
    )
    parser.add_argument(
        "--enable-change-monitor",
        action="store_true",
        default=False,
        help="Enable change detection monitoring",
    )
    parser.add_argument(
        "--enable-crawler",
        action="store_true",
        default=False,
        help="Enable focused web crawler",
    )
    parser.add_argument(
        "--enable-playwright",
        action="store_true",
        default=False,
        help="Enable Playwright browser automation",
    )
    parser.add_argument(
        "--enable-firecrawl",
        action="store_true",
        default=False,
        help="Enable Firecrawl scraping",
    )

    # Input / config overrides
    parser.add_argument(
        "--manual-signal-file",
        type=str,
        default=None,
        help="Path to manual signal JSON file",
    )
    parser.add_argument(
        "--source-registry",
        type=str,
        default=None,
        help="Path to source registry YAML file",
    )

    # Testing / debug flags
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Disable LLM calls (fallback/seed mode only)",
    )
    parser.add_argument(
        "--mock-web",
        action="store_true",
        default=False,
        help="Use mock web responses (testing mode)",
    )

    # Output
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/reports/perilla_discovery",
        help="Output directory for reports. Default: data/reports/perilla_discovery",
    )

    return parser


def _args_to_engine_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    """Convert parsed CLI arguments to AutonomousPerillaEngine constructor kwargs.

    Args:
        args: Parsed argparse Namespace.

    Returns:
        Dictionary of keyword arguments for AutonomousPerillaEngine.
    """
    return {
        "mode": args.mode,
        "topic": args.topic,
        "lookback_days": args.lookback_days,
        "max_concepts": args.max_concepts,
        "max_sources_per_concept": args.max_sources_per_concept,
        "max_candidates": args.max_candidates,
        "enable_search": args.enable_search,
        "enable_rss": args.enable_rss,
        "enable_change_monitor": args.enable_change_monitor,
        "enable_crawler": args.enable_crawler,
        "enable_playwright": args.enable_playwright,
        "enable_firecrawl": args.enable_firecrawl,
        "manual_signal_file": args.manual_signal_file,
        "source_registry_path": args.source_registry,
        "no_llm": args.no_llm,
        "mock_web": args.mock_web,
        "output_dir": args.output_dir,
    }


def _print_summary(summary: Dict[str, Any]) -> None:
    """Print a human-readable pipeline summary to stdout.

    Args:
        summary: Pipeline result dictionary with summary fields.
    """
    print("\n" + "=" * 60)
    print("  Perilla Discovery Pipeline Summary")
    print("=" * 60)
    print(f"  Mode:              {summary.get('mode', 'N/A')}")
    if summary.get("topic"):
        print(f"  Topic:             {summary['topic']}")
    print(f"  Concepts found:    {summary.get('concepts_found', 0)}")
    print(f"  Candidates found:  {summary.get('candidates_found', 0)}")
    print(f"  Bottlenecks found: {summary.get('bottlenecks_found', 0)}")
    print(f"  Review items:      {summary.get('review_items_count', 0)}")
    print(f"  Discovery items:   {summary.get('discovery_items_count', 0)}")
    print(f"  Elapsed:           {summary.get('elapsed_ms', 0)} ms")
    print(f"  Success:           {summary.get('success', False)}")
    if summary.get("error"):
        print(f"  Error:             {summary['error']}")
    print("=" * 60 + "\n")


def _write_report(report: PerillaDiscoveryReport, output_dir: str) -> None:
    """Write the discovery report to JSON and Markdown files.

    Args:
        report: PerillaDiscoveryReport instance.
        output_dir: Output directory path (relative or absolute).
    """
    # Resolve output directory
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(_PROJECT_ROOT, output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # JSON export
    json_path = os.path.join(output_dir, "perilla_discovery_latest.json")
    try:
        report_dict = asdict(report)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, ensure_ascii=False, indent=2, default=str)
        logger.info("Report JSON written to %s", json_path)
    except Exception as exc:
        logger.warning("Failed to write report JSON: %s", exc)

    # Markdown export
    md_path = os.path.join(output_dir, "perilla_discovery_latest.md")
    try:
        md_content = _render_report_markdown(report)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logger.info("Report Markdown written to %s", md_path)
    except Exception as exc:
        logger.warning("Failed to write report Markdown: %s", exc)


def _render_report_markdown(report: PerillaDiscoveryReport) -> str:
    """Render a PerillaDiscoveryReport as a Markdown document.

    Args:
        report: PerillaDiscoveryReport instance.

    Returns:
        Markdown string.
    """
    lines: list[str] = [
        "# Perilla Discovery Report",
        "",
        f"**Run ID**: {report.run_id}",
        f"**Mode**: {report.mode}",
        f"**Topic**: {report.input_topic or 'N/A'}",
        f"**Generated at**: {report.generated_at}",
        "",
        f"**Disclaimer**: {report.disclaimer}",
        "",
    ]

    # Statistics
    lines.append("## Statistics")
    lines.append("")
    lines.append(f"- Concept signals: {len(report.concept_signals)}")
    lines.append(f"- Demand anchors: {len(report.demand_anchors)}")
    lines.append(f"- Dynamic chains: {len(report.dynamic_chains)}")
    lines.append(f"- Bottleneck hypotheses: {len(report.bottleneck_hypotheses)}")
    lines.append(f"- Company candidates: {len(report.company_candidates)}")
    lines.append(f"- Discovery items: {len(report.discovery_items)}")
    lines.append(f"- Review queue: {len(report.review_queue)}")
    lines.append("")

    # Concept signals
    if report.concept_signals:
        lines.append("## Concept Signals")
        lines.append("")
        for cs in report.concept_signals[:20]:
            lines.append(
                f"- **{cs.name}** (heat={cs.heat_score:.2f}, "
                f"novelty={cs.novelty_score:.2f}, confidence={cs.confidence})"
            )
        lines.append("")

    # Bottleneck hypotheses
    if report.bottleneck_hypotheses:
        lines.append("## Bottleneck Hypotheses")
        lines.append("")
        for bh in report.bottleneck_hypotheses[:20]:
            lines.append(
                f"- **{bh.node_name}** (status={bh.status}, "
                f"score={bh.total_score}/60, "
                f"perilla_prob={bh.perilla_probability:.1%})"
            )
        lines.append("")

    # Discovery items
    if report.discovery_items:
        lines.append("## Discovery Items")
        lines.append("")
        lines.append("| Company | Classification | Evidence Level | Perilla Prob | Review Status |")
        lines.append("|---------|---------------|----------------|-------------|---------------|")
        for item in report.discovery_items[:30]:
            company = item.company_name or "N/A"
            lines.append(
                f"| {company} | {item.classification} | {item.evidence_level} "
                f"| {item.perilla_probability:.1%} | {item.review_status} |"
            )
        lines.append("")

    # Review queue
    if report.review_queue:
        lines.append("## Review Queue")
        lines.append("")
        for ri in report.review_queue[:20]:
            ri_dict = dict(ri) if isinstance(ri, Mapping) else asdict(ri)
            lines.append(
                f"- [{ri_dict.get('priority', 'N/A')}] "
                f"{ri_dict.get('reason', 'N/A')} -- "
                f"{ri_dict.get('required_action', 'N/A')}"
            )
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_pipeline()
    if not result.get("success"):
        sys.exit(1)
