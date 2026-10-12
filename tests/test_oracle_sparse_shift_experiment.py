from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from experiments.causal_mechanism_routing.oracle_sparse_shift import (
    ExperimentConfig,
    decide_go_no_go,
    run_experiment,
    summarize_results,
    write_results,
)


def test_oracle_local_preserves_stable_mechanisms_and_is_reproducible() -> None:
    config = ExperimentConfig(
        seeds=(0, 1, 2),
        changed_counts=(3,),
        sample_sizes=(16,),
        target_noise_levels=(1.0,),
    )

    first = run_experiment(config)
    second = run_experiment(config)

    assert first == second
    oracle = [row for row in first if row["method"] == "oracle_local"]
    assert all(row["forgetting_rate"] == 0.0 for row in oracle)
    assert all(row["noise_amplification"] == 1.0 for row in oracle)
    for seed in config.seeds:
        rows = {row["method"]: row for row in first if row["seed"] == seed}
        assert rows["oracle_local"]["ood_risk"] < rows["global"]["ood_risk"]


def test_summary_decision_and_artifacts_are_auditable(tmp_path: Path) -> None:
    config = ExperimentConfig(
        seeds=tuple(range(10)),
        changed_counts=(1, 3, 5),
        sample_sizes=(16,),
        target_noise_levels=(0.5, 1.0, 2.0),
    )
    records = run_experiment(config)
    summaries = summarize_results(records)
    decision = decide_go_no_go(records, config)

    assert decision["decision"] in {"GO", "NO_GO"}
    assert len(decision["conditions"]) == 9

    write_results(tmp_path, config, records, summaries, decision)

    with (tmp_path / "records.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == len(records)
    saved_decision = json.loads((tmp_path / "decision.json").read_text(encoding="utf-8"))
    assert saved_decision == decision
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["protocol_version"] == "p1-oracle-v0.1"
    assert manifest["record_count"] == len(records)
    assert manifest["artifact_sha256"]["records.csv"] == hashlib.sha256(
        (tmp_path / "records.csv").read_bytes()
    ).hexdigest()
    assert manifest["artifact_sha256"]["summary.csv"] == hashlib.sha256(
        (tmp_path / "summary.csv").read_bytes()
    ).hexdigest()
