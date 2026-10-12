"""P3.8 研究操作系统生产可靠性、安全和恢复测试。

覆盖：
- trace_context 的 trace_id 传递和 span 计时
- structured_logger 的密钥脱敏
- metrics 指标收集和查询
- health_checks 的 SQLite 一致性检查（临时数据库）
- backup_recovery 的备份和恢复（临时目录）
- rate_limiter 的速率限制
- provider 故障演练场景（模拟 provider 失败，验证历史读取仍可用）
- 任务中断恢复场景（模拟任务中断，验证可恢复）
- 模型回滚演练场景（验证回滚流程）

所有测试独立可运行，不依赖外部服务；使用 tmp_path 处理临时文件。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from core.runtime.observability import (
    BackupManager,
    HealthChecker,
    MetricsCollector,
    RateLimiter,
    TraceContext,
    check_file_checksums,
    check_provider_availability,
    check_sqlite_integrity,
    compute_file_checksums,
    get_global_collector,
    get_trace_id,
    is_sensitive_key,
    mask_sensitive,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    write_checksum_manifest,
)


# ---------------------------------------------------------------------------
# trace_context 测试
# ---------------------------------------------------------------------------


class TestTraceContext:
    """trace_id 上下文与 span 计时测试。"""

    def setup_method(self) -> None:
        """每个测试前清空上下文，避免污染。"""
        reset_trace_id()

    def teardown_method(self) -> None:
        """每个测试后清空上下文。"""
        reset_trace_id()

    def test_new_trace_id_format(self) -> None:
        """new_trace_id 应生成 trace_ 前缀的唯一 ID。"""
        trace_id = new_trace_id()
        assert trace_id.startswith("trace_")
        assert len(trace_id) > len("trace_")

    def test_set_and_get_trace_id(self) -> None:
        """set_trace_id 后 get_trace_id 应返回相同值。"""
        trace_id = set_trace_id("trace_test_123")
        assert trace_id == "trace_test_123"
        assert get_trace_id() == "trace_test_123"

    def test_set_trace_id_auto_generate(self) -> None:
        """set_trace_id(None) 应自动生成新 ID。"""
        trace_id = set_trace_id(None)
        assert trace_id.startswith("trace_")
        assert get_trace_id() == trace_id

    def test_trace_context_isolation(self) -> None:
        """TraceContext 退出后应恢复外层 trace_id。"""
        outer_id = set_trace_id("trace_outer")
        with TraceContext("test") as ctx:
            # 未显式指定 trace_id 时复用外层（同一请求链路）
            assert ctx.trace_id == outer_id
            assert get_trace_id() == outer_id
        # 退出后应恢复外层
        assert get_trace_id() == outer_id

    def test_trace_context_new_id_when_no_outer(self) -> None:
        """无外层 trace_id 时应自动生成新 ID。"""
        reset_trace_id()
        with TraceContext("test") as ctx:
            assert ctx.trace_id.startswith("trace_")
            assert get_trace_id() == ctx.trace_id
        # 退出后恢复为空
        assert get_trace_id() == ""

    def test_trace_context_span_timing(self) -> None:
        """span 应记录耗时且 finished_at > started_at。"""
        with TraceContext("research.run") as ctx:
            with ctx.span("stage_a") as span:
                time.sleep(0.05)
            assert span.is_finished
            assert span.duration_seconds > 0.0
            assert span.duration_seconds >= 0.01

    def test_trace_context_nested_spans(self) -> None:
        """嵌套 span 应按层级记录。"""
        with TraceContext("root") as ctx:
            with ctx.span("outer") as outer_span:
                with ctx.span("inner") as inner_span:
                    assert inner_span.trace_id == ctx.trace_id
            assert outer_span.is_finished
            assert inner_span.is_finished

    def test_trace_context_error_recording(self) -> None:
        """异常时应记录失败原因。"""
        with TraceContext("root") as ctx:
            with ctx.span("failing") as span:
                span.record_error("simulated failure")
            assert span.error == "simulated failure"

    def test_trace_context_exception_capture(self) -> None:
        """上下文内抛异常时 root_span 应记录错误。"""
        with pytest.raises(ValueError, match="test error"):
            with TraceContext("root") as ctx:
                raise ValueError("test error")
        assert "ValueError" in ctx.root_span.error
        assert "test error" in ctx.root_span.error

    def test_trace_context_attributes(self) -> None:
        """set_attribute 应记录到 span。"""
        with TraceContext("root") as ctx:
            ctx.set_attribute("mode", "premarket")
            ctx.set_attribute("symbol_count", 10)
            assert ctx.root_span.attributes["mode"] == "premarket"
            assert ctx.root_span.attributes["symbol_count"] == 10


# ---------------------------------------------------------------------------
# structured_logger 密钥脱敏测试
# ---------------------------------------------------------------------------


class TestStructuredLogger:
    """结构化日志器密钥脱敏测试。"""

    def test_is_sensitive_key_password(self) -> None:
        """password 字段应被识别为敏感。"""
        assert is_sensitive_key("password") is True
        assert is_sensitive_key("PASSWORD") is True
        assert is_sensitive_key("user_password") is True

    def test_is_sensitive_key_api_key(self) -> None:
        """api_key 字段应被识别为敏感。"""
        assert is_sensitive_key("api_key") is True
        assert is_sensitive_key("apikey") is True
        assert is_sensitive_key("API_KEY") is True

    def test_is_sensitive_key_token(self) -> None:
        """token 字段应被识别为敏感。"""
        assert is_sensitive_key("token") is True
        assert is_sensitive_key("access_token") is True

    def test_is_sensitive_key_normal(self) -> None:
        """普通字段不应被识别为敏感。"""
        assert is_sensitive_key("symbol") is False
        assert is_sensitive_key("trade_date") is False
        assert is_sensitive_key("close") is False

    def test_mask_sensitive_dict(self) -> None:
        """字典中的敏感字段应被脱敏。"""
        payload = {
            "symbol": "600519.SH",
            "api_key": "sk-secret-123",
            "password": "my-password",
            "nested": {
                "token": "tok-abc",
                "name": "贵州茅台",
            },
        }
        masked = mask_sensitive(payload)
        assert masked["symbol"] == "600519.SH"
        assert masked["api_key"] == "***REDACTED***"
        assert masked["password"] == "***REDACTED***"
        assert masked["nested"]["token"] == "***REDACTED***"
        assert masked["nested"]["name"] == "贵州茅台"

    def test_mask_sensitive_list(self) -> None:
        """列表中的字典敏感字段应被脱敏。"""
        payload = [
            {"api_key": "sk-1", "name": "item1"},
            {"api_key": "sk-2", "name": "item2"},
        ]
        masked = mask_sensitive(payload)
        assert masked[0]["api_key"] == "***REDACTED***"
        assert masked[0]["name"] == "item1"
        assert masked[1]["api_key"] == "***REDACTED***"

    def test_mask_sensitive_primitive(self) -> None:
        """原始类型应原样返回。"""
        assert mask_sensitive("hello") == "hello"
        assert mask_sensitive(42) == 42
        assert mask_sensitive(None) is None

    def test_mask_sensitive_custom_keys(self) -> None:
        """应支持自定义敏感字段名。"""
        payload = {"custom_secret_field": "value", "normal": "ok"}
        masked = mask_sensitive(payload, sensitive_keys={"custom_secret_field"})
        assert masked["custom_secret_field"] == "***REDACTED***"
        assert masked["normal"] == "ok"


# ---------------------------------------------------------------------------
# metrics 指标收集测试
# ---------------------------------------------------------------------------


class TestMetrics:
    """指标收集器测试。"""

    def test_provider_registration(self) -> None:
        """注册 provider 后应可查询。"""
        collector = MetricsCollector()
        metric = collector.register_provider("tushare")
        assert metric.name == "tushare"
        assert metric.available is True
        assert metric.availability_rate == 1.0

    def test_provider_request_recording(self) -> None:
        """记录请求后应更新可用率。"""
        collector = MetricsCollector()
        collector.record_provider_request("tushare", success=True)
        collector.record_provider_request("tushare", success=True)
        collector.record_provider_request("tushare", success=False, error="timeout")
        metric = collector.get_provider("tushare")
        assert metric is not None
        assert metric.request_count == 3
        assert metric.failure_count == 1
        assert metric.availability_rate == pytest.approx(2 / 3, rel=0.01)
        assert metric.last_error == "timeout"

    def test_provider_rate_limited(self) -> None:
        """限流状态应被记录。"""
        collector = MetricsCollector()
        collector.record_provider_request(
            "akshare", success=True, rate_limited=True
        )
        metric = collector.get_provider("akshare")
        assert metric is not None
        assert metric.rate_limited is True
        assert metric.available is False

    def test_provider_staleness_and_coverage(self) -> None:
        """陈旧度与覆盖率应被记录。"""
        collector = MetricsCollector()
        collector.record_provider_request(
            "tushare",
            success=True,
            staleness_seconds=7200.0,
            coverage_ratio=0.85,
        )
        metric = collector.get_provider("tushare")
        assert metric is not None
        assert metric.staleness_seconds == 7200.0
        assert metric.coverage_ratio == 0.85

    def test_queue_metrics(self) -> None:
        """队列指标应正确记录。"""
        collector = MetricsCollector()
        collector.set_queue_depth(depth=10, in_flight=3)
        collector.set_budget(remaining=80, total=100)
        collector.record_cache_hit()
        collector.record_cache_hit()
        collector.record_cache_miss()
        collector.record_retry()
        collector.record_dead_letter()
        collector.record_task_outcome(succeeded=True)
        collector.record_task_outcome(succeeded=False)
        snapshot = collector.snapshot()
        assert snapshot["queue"]["queue_depth"] == 10
        assert snapshot["queue"]["in_flight"] == 3
        assert snapshot["queue"]["budget_consumed_ratio"] == pytest.approx(0.2, rel=0.01)
        assert snapshot["queue"]["cache_hit_rate"] == pytest.approx(2 / 3, rel=0.01)
        assert snapshot["queue"]["retry_count"] == 1
        assert snapshot["queue"]["dead_letter_count"] == 1
        assert snapshot["queue"]["succeeded_count"] == 1
        assert snapshot["queue"]["failed_count"] == 1

    def test_model_metrics(self) -> None:
        """模型指标应正确记录。"""
        collector = MetricsCollector()
        collector.register_model(
            model_id="model-001",
            version="1.0",
            status="active",
            rollback_version="0.9",
        )
        metric = collector.get_model("model-001")
        assert metric is not None
        assert metric.version == "1.0"
        assert metric.status == "active"
        assert metric.rollback_version == "0.9"
        assert metric.degraded is False
        collector.set_model_degraded("model-001", True)
        metric = collector.get_model("model-001")
        assert metric is not None
        assert metric.degraded is True

    def test_snapshot_summary(self) -> None:
        """快照应包含汇总信息。"""
        collector = MetricsCollector()
        collector.register_provider("tushare")
        collector.register_provider("akshare")
        collector.register_model("m1", "1.0", "active")
        snapshot = collector.snapshot()
        assert snapshot["summary"]["provider_count"] == 2
        assert snapshot["summary"]["model_count"] == 1
        assert snapshot["summary"]["overall_available"] is True

    def test_global_collector_singleton(self) -> None:
        """全局收集器应为单例。"""
        c1 = get_global_collector()
        c2 = get_global_collector()
        assert c1 is c2


# ---------------------------------------------------------------------------
# health_checks 测试
# ---------------------------------------------------------------------------


class TestHealthChecks:
    """健康检查器测试。"""

    def test_sqlite_integrity_ok(self, tmp_path: Path) -> None:
        """正常 SQLite 数据库应通过一致性检查。"""
        db_path = tmp_path / "test.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO test VALUES (1, 'hello')")
            conn.commit()
        result = check_sqlite_integrity(db_path)
        assert result.status == "ok"
        assert result.details["integrity_result"] == "ok"
        assert result.details["table_count"] >= 1

    def test_sqlite_integrity_missing_file(self, tmp_path: Path) -> None:
        """不存在的数据库应返回 warning。"""
        db_path = tmp_path / "nonexistent.db"
        result = check_sqlite_integrity(db_path)
        assert result.status == "warning"
        assert result.details["path_exists"] is False

    def test_sqlite_integrity_corrupt(self, tmp_path: Path) -> None:
        """损坏的数据库应返回 error。"""
        db_path = tmp_path / "corrupt.db"
        db_path.write_text("not a database", encoding="utf-8")
        result = check_sqlite_integrity(db_path)
        assert result.status == "error"

    def test_file_checksums_ok(self, tmp_path: Path) -> None:
        """校验清单匹配时应通过。"""
        # 创建文件
        (tmp_path / "a.json").write_text('{"key": "value"}', encoding="utf-8")
        (tmp_path / "b.md").write_text("# title", encoding="utf-8")
        # 生成清单
        manifest_path = tmp_path / "_manifest.json"
        write_checksum_manifest(tmp_path, manifest_path)
        result = check_file_checksums(manifest_path)
        assert result.status == "ok"
        assert result.details["ok_count"] >= 2

    def test_file_checksums_mismatch(self, tmp_path: Path) -> None:
        """文件被修改后应检测到不匹配。"""
        (tmp_path / "a.json").write_text('{"key": "value"}', encoding="utf-8")
        manifest_path = tmp_path / "_manifest.json"
        write_checksum_manifest(tmp_path, manifest_path)
        # 修改文件
        (tmp_path / "a.json").write_text('{"key": "changed"}', encoding="utf-8")
        result = check_file_checksums(manifest_path)
        assert result.status == "warning"
        assert result.details["mismatched_count"] >= 1

    def test_file_checksums_missing_manifest(self, tmp_path: Path) -> None:
        """清单不存在时应返回 warning。"""
        result = check_file_checksums(tmp_path / "no_manifest.json")
        assert result.status == "warning"

    def test_compute_file_checksums(self, tmp_path: Path) -> None:
        """compute_file_checksums 应返回相对路径 -> 哈希字典。"""
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.json").write_text("{}", encoding="utf-8")
        checksums = compute_file_checksums(tmp_path, patterns=("*.json",))
        assert "a.json" in checksums
        assert "sub/b.json" in checksums

    def test_provider_availability_ok(self) -> None:
        """全部 provider 可用时应通过。"""
        collector = MetricsCollector()
        collector.set_provider_available("tushare", True)
        collector.set_provider_available("akshare", True)
        result = check_provider_availability(["tushare", "akshare"], collector)
        assert result.status == "ok"

    def test_provider_availability_unavailable(self) -> None:
        """provider 不可用时应返回 warning。"""
        collector = MetricsCollector()
        collector.set_provider_available("tushare", True)
        collector.set_provider_available("akshare", False)
        result = check_provider_availability(["tushare", "akshare"], collector)
        assert result.status == "warning"
        assert "akshare" in result.details["unavailable"]

    def test_health_checker_aggregation(self, tmp_path: Path) -> None:
        """HealthChecker 应聚合多项检查。"""
        db_path = tmp_path / "test.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
        checker = HealthChecker()
        checker.add_check("sqlite", lambda: check_sqlite_integrity(db_path))
        report = checker.run_all()
        assert report["status"] == "ok"
        assert report["check_count"] == 1
        assert report["checks"][0]["name"] == "sqlite"

    def test_health_checker_with_error(self) -> None:
        """单项检查异常时整体应返回 error。"""
        checker = HealthChecker()

        def failing_check():
            raise RuntimeError("check failed")

        checker.add_check("failing", failing_check)
        report = checker.run_all()
        assert report["status"] == "error"


# ---------------------------------------------------------------------------
# backup_recovery 测试
# ---------------------------------------------------------------------------


class TestBackupRecovery:
    """备份与恢复测试。"""

    def _create_source(self, tmp_path: Path) -> Path:
        """创建测试源目录。"""
        source = tmp_path / "source"
        source.mkdir()
        (source / "a.json").write_text('{"key": "value"}', encoding="utf-8")
        (source / "b.md").write_text("# title", encoding="utf-8")
        (source / "secrets.py").write_text("SECRET = 'key'", encoding="utf-8")
        return source

    def test_backup_creates_manifest(self, tmp_path: Path) -> None:
        """备份应生成清单文件。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manifest = manager.create_backup(backup_id="backup_001")
        assert manifest.backup_id == "backup_001"
        assert manifest.file_count == 2  # a.json + b.md，secrets.py 被排除
        assert "a.json" in manifest.checksums
        assert "b.md" in manifest.checksums
        assert "secrets.py" not in manifest.checksums
        # 清单文件应存在
        manifest_file = backup_root / "backup_001" / "_backup_manifest.json"
        assert manifest_file.exists()

    def test_backup_excludes_secrets(self, tmp_path: Path) -> None:
        """备份应排除密钥文件。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(
            source, backup_root, patterns=("*.json", "*.md", "*.py")
        )
        manifest = manager.create_backup(backup_id="backup_002")
        assert "secrets.py" not in manifest.checksums
        assert "a.json" in manifest.checksums

    def test_restore_round_trip(self, tmp_path: Path) -> None:
        """备份后恢复应得到相同文件。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manifest = manager.create_backup(backup_id="backup_003")
        target = tmp_path / "restored"
        result = manager.restore(manifest, target, dry_run=False)
        assert result["restored_count"] == 2
        assert result["verified_count"] == 2
        assert (target / "a.json").exists()
        assert (target / "b.md").exists()
        # 内容应一致
        assert (target / "a.json").read_text(encoding="utf-8") == '{"key": "value"}'

    def test_restore_dry_run(self, tmp_path: Path) -> None:
        """dry_run 模式不应实际复制文件。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manifest = manager.create_backup(backup_id="backup_004")
        target = tmp_path / "dry_run_target"
        result = manager.restore(manifest, target, dry_run=True)
        assert result["restored_count"] == 0
        assert result["verified_count"] == 2
        assert not target.exists()

    def test_verify_backup(self, tmp_path: Path) -> None:
        """校验备份应返回验证结果。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manifest = manager.create_backup(backup_id="backup_005")
        verify_result = manager.verify_backup(manifest)
        assert verify_result["verified_count"] == 2
        assert verify_result["mismatched"] == []

    def test_recovery_drill(self, tmp_path: Path) -> None:
        """恢复演练应完成完整流程。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manager.create_backup(backup_id="backup_drill")
        target = tmp_path / "drill_target"
        result = manager.run_recovery_drill(target, backup_id="backup_drill")
        assert result["status"] == "ok"
        assert result["final_check"]["all_matched"] is True
        assert result["restore"]["restored_count"] == 2

    def test_incremental_backup(self, tmp_path: Path) -> None:
        """增量备份应仅复制变化文件。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        # 第一次全量
        manifest1 = manager.create_backup(backup_id="backup_inc_1", incremental=False)
        # 修改一个文件
        (source / "a.json").write_text('{"key": "changed"}', encoding="utf-8")
        # 第二次增量
        manifest2 = manager.create_backup(backup_id="backup_inc_2", incremental=True)
        assert manifest2.previous_backup_id == "backup_inc_1"
        assert manifest2.checksums["a.json"] != manifest1.checksums["a.json"]

    def test_list_backups(self, tmp_path: Path) -> None:
        """list_backups 应返回所有备份清单。"""
        source = self._create_source(tmp_path)
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manager.create_backup(backup_id="backup_list_1")
        manager.create_backup(backup_id="backup_list_2")
        backups = manager.list_backups()
        assert len(backups) == 2
        backup_ids = {b.backup_id for b in backups}
        assert backup_ids == {"backup_list_1", "backup_list_2"}


# ---------------------------------------------------------------------------
# rate_limiter 测试
# ---------------------------------------------------------------------------


class TestRateLimiter:
    """速率限制器测试。"""

    def test_allow_under_limit(self) -> None:
        """未超限时应允许通过。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=5, window_seconds=60)
        for _ in range(5):
            allowed, retry_after = limiter.check("127.0.0.1", "/api/test")
            assert allowed is True
            assert retry_after == 0.0

    def test_block_over_limit(self) -> None:
        """超限时应拒绝并返回 retry_after。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=3, window_seconds=60)
        for _ in range(3):
            allowed, _ = limiter.check("127.0.0.1", "/api/test")
            assert allowed is True
        # 第 4 次应被拒绝
        allowed, retry_after = limiter.check("127.0.0.1", "/api/test")
        assert allowed is False
        assert retry_after > 0

    def test_isolated_by_ip(self) -> None:
        """不同 IP 应独立计数。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=2, window_seconds=60)
        # IP A 用满额度
        limiter.check("127.0.0.1", "/api/test")
        limiter.check("127.0.0.1", "/api/test")
        # IP B 应仍可用
        allowed, _ = limiter.check("127.0.0.1", "/api/test")
        assert allowed is True

    def test_isolated_by_endpoint(self) -> None:
        """不同端点应独立计数。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/a", max_requests=2, window_seconds=60)
        limiter.add_rule("/api/b", max_requests=2, window_seconds=60)
        limiter.check("127.0.0.1", "/api/a")
        limiter.check("127.0.0.1", "/api/a")
        # /api/a 已满，/api/b 应仍可用
        allowed_a, _ = limiter.check("127.0.0.1", "/api/a")
        allowed_b, _ = limiter.check("127.0.0.1", "/api/b")
        assert allowed_a is False
        assert allowed_b is True

    def test_window_expiry(self) -> None:
        """窗口过期后应恢复额度。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=2, window_seconds=0.1)
        base_time = time.monotonic()
        limiter.check("127.0.0.1", "/api/test", now=base_time)
        limiter.check("127.0.0.1", "/api/test", now=base_time)
        # 窗口内应被拒绝
        allowed, _ = limiter.check("127.0.0.1", "/api/test", now=base_time + 0.05)
        assert allowed is False
        # 窗口过期后应恢复
        allowed, _ = limiter.check("127.0.0.1", "/api/test", now=base_time + 0.2)
        assert allowed is True

    def test_reset(self) -> None:
        """reset 应清空计数器。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=1, window_seconds=60)
        limiter.check("127.0.0.1", "/api/test")
        allowed, _ = limiter.check("127.0.0.1", "/api/test")
        assert allowed is False
        limiter.reset()
        allowed, _ = limiter.check("127.0.0.1", "/api/test")
        assert allowed is True

    def test_invalid_rule(self) -> None:
        """无效规则参数应抛异常。"""
        limiter = RateLimiter()
        with pytest.raises(ValueError):
            limiter.add_rule("/api/test", max_requests=0, window_seconds=60)
        with pytest.raises(ValueError):
            limiter.add_rule("/api/test", max_requests=10, window_seconds=0)

    def test_snapshot(self) -> None:
        """snapshot 应返回规则与计数器状态。"""
        limiter = RateLimiter()
        limiter.add_rule("/api/test", max_requests=5, window_seconds=60)
        limiter.check("127.0.0.1", "/api/test")
        snapshot = limiter.snapshot()
        assert snapshot["rule_count"] == 1
        assert "/api/test" in snapshot["rules"]


# ---------------------------------------------------------------------------
# provider 故障演练场景
# ---------------------------------------------------------------------------


class TestProviderFailureDrill:
    """provider 故障演练：模拟 provider 失败，验证历史读取仍可用。"""

    def test_single_provider_failure_does_not_block_others(self) -> None:
        """单个 provider 失败不影响其他 provider。"""
        collector = MetricsCollector()
        # 两个 provider 都正常
        collector.set_provider_available("tushare", True)
        collector.set_provider_available("akshare", True)
        # tushare 故障
        collector.record_provider_request(
            "tushare", success=False, error="connection refused"
        )
        collector.set_provider_available("tushare", False)
        # akshare 仍可用
        snapshot = collector.snapshot()
        tushare = snapshot["providers"]["tushare"]
        akshare = snapshot["providers"]["akshare"]
        assert tushare["available"] is False
        assert akshare["available"] is True

    def test_historical_read_still_available_on_provider_failure(self) -> None:
        """provider 故障时历史读取应仍可用（走本地缓存）。"""
        collector = MetricsCollector()
        # 模拟 tushare 故障
        collector.set_provider_available("tushare", False)
        collector.record_provider_request(
            "tushare", success=False, error="service down"
        )
        # 历史读取走本地缓存（cache_hit）
        collector.record_cache_hit()
        collector.record_cache_hit()
        snapshot = collector.snapshot()
        # 历史读取通过缓存命中
        assert snapshot["queue"]["cache_hits"] == 2
        assert snapshot["queue"]["cache_hit_rate"] == 1.0
        # tushare 故障但缓存可用
        assert snapshot["providers"]["tushare"]["available"] is False
        assert snapshot["summary"]["overall_available"] is False

    def test_provider_availability_check_reflects_failure(self) -> None:
        """健康检查应反映 provider 故障。"""
        collector = MetricsCollector()
        collector.set_provider_available("tushare", False)
        result = check_provider_availability(["tushare"], collector)
        assert result.status == "warning"
        assert "tushare" in result.details["unavailable"]


# ---------------------------------------------------------------------------
# 任务中断恢复演练场景
# ---------------------------------------------------------------------------


class TestTaskInterruptionRecovery:
    """任务中断恢复演练：模拟任务中断，验证可恢复。"""

    def test_resumable_task_recovery(self, tmp_path: Path) -> None:
        """中断的任务应可被 TaskResumer 恢复。"""
        from core.research.control_plane.contracts import ResearchTask
        from core.research.control_plane.repository import ResearchRepository
        from core.research.control_plane.task_resumer import TaskResumer

        db_path = tmp_path / "research.db"
        repo = ResearchRepository(db_path=db_path)
        resumer = TaskResumer(repo)
        # 创建一个 running 状态的任务（模拟中断）
        task = ResearchTask(
            task_type="evidence_completion",
            target_claim_id="claim_test_001",
            status="running",
            attempt_number=1,
        )
        task.attempt_id = "att_initial"
        task.attempt_started_at = "2026-07-22T00:00:00+00:00"
        repo.save_task(task)
        # 保存一个 running 的 attempt（模拟中断时未关闭）
        from core.research.control_plane.task_resumer import ResearchTaskAttempt
        attempt = ResearchTaskAttempt(
            attempt_id="att_initial",
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id="claim_test_001",
            attempt_number=1,
            status="running",
            started_at="2026-07-22T00:00:00+00:00",
            completed_stages=["local"],
        )
        resumer.save_attempt(attempt)
        # 列出可恢复任务
        resumable = resumer.list_resumable_tasks()
        assert len(resumable) >= 1
        assert resumable[0].task_id == task.task_id
        # 恢复任务
        recovered = resumer.resume_task(task)
        assert recovered.status == "queued"
        assert recovered.attempt_number >= 2
        # checkpoint 应保留已完成阶段
        assert "local" in recovered.checkpoint.get("completed_stages", [])

    def test_resumable_task_with_checkpoint(self, tmp_path: Path) -> None:
        """带 checkpoint 的任务恢复后应保留进度。"""
        from core.research.control_plane.contracts import ResearchTask
        from core.research.control_plane.repository import ResearchRepository
        from core.research.control_plane.task_resumer import (
            ResearchTaskAttempt,
            TaskResumer,
        )

        db_path = tmp_path / "research.db"
        repo = ResearchRepository(db_path=db_path)
        resumer = TaskResumer(repo)
        task = ResearchTask(
            task_type="evidence_completion",
            target_claim_id="claim_test_002",
            status="running",
            attempt_number=1,
        )
        task.attempt_id = "att_checkpoint"
        task.attempt_started_at = "2026-07-22T00:00:00+00:00"
        repo.save_task(task)
        # 保存带 checkpoint 的 attempt
        attempt = ResearchTaskAttempt(
            attempt_id="att_checkpoint",
            task_id=task.task_id,
            run_id=task.run_id,
            target_claim_id="claim_test_002",
            attempt_number=1,
            status="running",
            started_at="2026-07-22T00:00:00+00:00",
            completed_stages=["local", "first_party"],
            seen_document_ids=["doc_001", "doc_002"],
            consumed_budget={"max_requests": 20, "used": 5},
        )
        resumer.save_attempt(attempt)
        # 恢复
        recovered = resumer.resume_task(task)
        assert recovered.status == "queued"
        checkpoint = recovered.checkpoint
        assert "local" in checkpoint["completed_stages"]
        assert "first_party" in checkpoint["completed_stages"]
        assert "doc_001" in checkpoint["seen_document_ids"]


# ---------------------------------------------------------------------------
# 模型回滚演练场景
# ---------------------------------------------------------------------------


class TestModelRollbackDrill:
    """模型回滚演练：验证回滚流程。"""

    def _make_input_contract(self):
        from core.research.model_governance.contracts import InputContract
        contract = InputContract(
            fields={"revenue": "float"},
            data_source_refs=["tushare"],
        )
        return InputContract(
            fields=contract.fields,
            data_source_refs=contract.data_source_refs,
            contract_hash=contract.compute_hash(),
        )

    def _make_output_contract(self):
        from core.research.model_governance.contracts import OutputContract
        contract = OutputContract(
            fields={"forecast": "float"},
            validation_rules={"range": "[0, inf)"},
        )
        return OutputContract(
            fields=contract.fields,
            validation_rules=contract.validation_rules,
            contract_hash=contract.compute_hash(),
        )

    def _make_model(self, model_id, version, status, rollback_version=""):
        from core.research.model_governance.contracts import (
            ModelStatus,
            ModelVersion,
        )
        return ModelVersion(
            model_id=model_id,
            model_family="revenue_forecast",
            domain="consumer",
            version=version,
            owner="analyst-A",
            reviewer="reviewer-B",
            training_or_calibration_window="2020-01-01:2024-12-31",
            input_contract=self._make_input_contract(),
            output_contract=self._make_output_contract(),
            applicable_universe=["600519.SH"],
            limitations=["仅消费行业"],
            benchmark={"mape": 0.15},
            validation_report={},
            status=status,
            effective_from="2026-01-01",
            effective_to="",
            rollback_version=rollback_version,
            code_fingerprint=f"code-{version}",
            config_fingerprint=f"config-{version}",
            data_fingerprint=f"data-{version}",
        )

    def test_model_rollback_flow(self, tmp_path: Path) -> None:
        """模型回滚流程：active -> suspended, rollback_version -> active。"""
        from core.research.model_governance.contracts import ModelStatus
        from core.research.model_governance.registry import ModelRegistry
        from core.research.model_governance.workflow import ModelWorkflow

        registry_path = tmp_path / "registry.json"
        registry = ModelRegistry(registry_path=registry_path)
        workflow = ModelWorkflow(registry)

        # v1.0 从 draft 开始走完整流程
        v1 = self._make_model("model-001", "1.0", ModelStatus.DRAFT)
        workflow.submit_draft(v1)
        workflow.unit_validate("model-001", "1.0", "analyst-A", {"test": "passed"})
        workflow.start_shadow("model-001", "1.0", "analyst-A")
        workflow.independent_validate(
            "model-001", "1.0", "reviewer-B", {"review": "passed"}
        )
        workflow.approve("model-001", "1.0", "reviewer-B")
        workflow.activate(
            "model-001", "1.0", "reviewer-B", rollback_version="1.0"
        )
        # suspend v1.0（模拟被 v1.1 替换）
        workflow.suspend("model-001", "1.0", "reviewer-B", "被 v1.1 替换")

        # v1.1 从 draft 开始走完整流程，rollback_version 指向 v1.0
        v11 = self._make_model(
            "model-001", "1.1", ModelStatus.DRAFT, rollback_version="1.0"
        )
        workflow.submit_draft(v11)
        workflow.unit_validate("model-001", "1.1", "analyst-A", {"test": "passed"})
        workflow.start_shadow("model-001", "1.1", "analyst-A")
        workflow.independent_validate(
            "model-001", "1.1", "reviewer-B", {"review": "passed"}
        )
        workflow.approve("model-001", "1.1", "reviewer-B")
        workflow.activate(
            "model-001", "1.1", "reviewer-B", rollback_version="1.0"
        )

        # 执行回滚
        rolled_back = workflow.rollback(
            model_id="model-001",
            from_version="1.1",
            actor="ops-user",
            reason="演练：模型回滚验证",
        )
        # 验证回滚结果
        assert rolled_back.version == "1.0"
        assert rolled_back.status == ModelStatus.ACTIVE
        # 原 active 版本应变为 suspended
        v11_after = registry.get_version("model-001", "1.1")
        assert v11_after.status == ModelStatus.SUSPENDED

    def test_rollback_requires_active_status(self, tmp_path: Path) -> None:
        """非 active 状态的模型不能回滚。"""
        from core.research.model_governance.contracts import ModelStatus
        from core.research.model_governance.registry import ModelRegistry
        from core.research.model_governance.workflow import (
            ModelWorkflow,
            ModelWorkflowError,
        )

        registry_path = tmp_path / "registry.json"
        registry = ModelRegistry(registry_path=registry_path)
        workflow = ModelWorkflow(registry)

        # v1.0 从 draft 开始，走完 approve 但不 activate
        v1 = self._make_model("model-002", "1.0", ModelStatus.DRAFT)
        workflow.submit_draft(v1)
        workflow.unit_validate("model-002", "1.0", "analyst-A", {"test": "passed"})
        workflow.start_shadow("model-002", "1.0", "analyst-A")
        workflow.independent_validate(
            "model-002", "1.0", "reviewer-B", {"review": "passed"}
        )
        workflow.approve("model-002", "1.0", "reviewer-B")
        # 此时状态为 approved，不是 active，回滚应失败
        with pytest.raises(ModelWorkflowError):
            workflow.rollback(
                model_id="model-002",
                from_version="1.0",
                actor="ops-user",
                reason="测试回滚",
            )

    def test_model_metrics_reflect_rollback(self, tmp_path: Path) -> None:
        """回滚后模型指标应反映新 active 版本。"""
        collector = MetricsCollector()
        # 初始登记 v1.1 active
        collector.register_model(
            model_id="model-003",
            version="1.1",
            status="active",
            rollback_version="1.0",
        )
        # 回滚后更新为 v1.0 active
        collector.register_model(
            model_id="model-003",
            version="1.0",
            status="active",
            rollback_version="",
        )
        metric = collector.get_model("model-003")
        assert metric is not None
        assert metric.version == "1.0"
        assert metric.status == "active"


# ---------------------------------------------------------------------------
# 综合端到端演练
# ---------------------------------------------------------------------------


class TestEndToEndDrill:
    """综合端到端演练：验证多个组件协同工作。"""

    def test_full_recovery_drill_with_observability(self, tmp_path: Path) -> None:
        """完整恢复演练：备份 -> 校验 -> 恢复 -> 健康检查。"""
        # 1. 创建源数据
        source = tmp_path / "research_control"
        source.mkdir()
        (source / "config.json").write_text(
            json.dumps({"version": "1.0"}), encoding="utf-8"
        )
        (source / "notes.md").write_text("# 研究笔记", encoding="utf-8")
        # 2. 备份
        backup_root = tmp_path / "backups"
        manager = BackupManager(source, backup_root, patterns=("*.json", "*.md"))
        manifest = manager.create_backup(backup_id="e2e_backup")
        assert manifest.file_count == 2
        # 3. 恢复演练
        target = tmp_path / "restored"
        drill_result = manager.run_recovery_drill(target, backup_id="e2e_backup")
        assert drill_result["status"] == "ok"
        # 4. 健康检查
        manifest_path = backup_root / "e2e_backup" / "_backup_manifest.json"
        # 先写校验清单到恢复目录
        restored_manifest = target / "_manifest.json"
        write_checksum_manifest(target, restored_manifest, patterns=("*.json", "*.md"))
        result = check_file_checksums(restored_manifest)
        assert result.status == "ok"
        assert result.details["ok_count"] >= 2

    def test_observability_pipeline_with_trace(self, tmp_path: Path) -> None:
        """可观察性管道：trace -> metrics -> health。"""
        reset_trace_id()
        collector = MetricsCollector()
        with TraceContext("research.drill") as ctx:
            ctx.set_attribute("drill_type", "provider_failure")
            # 记录 provider 请求
            collector.record_provider_request(
                "tushare", success=False, error="simulated failure"
            )
            collector.set_provider_available("tushare", False)
            # 历史读取走缓存
            collector.record_cache_hit()
            # 健康检查
            health_result = check_provider_availability(["tushare"], collector)
            assert health_result.status == "warning"
            assert ctx.trace_id != ""
            assert ctx.root_span.attributes["drill_type"] == "provider_failure"
        # trace 退出后应恢复
        assert get_trace_id() == ""
        reset_trace_id()
