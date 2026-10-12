"""
Strategy parameter optimization engine supporting Genetic Algorithm (GA) and
Bayesian Optimization (BO) using Optuna.

Dependencies:
    - deap (pip install deap)
    - optuna (pip install optuna)
    - pandas, numpy
"""

from __future__ import annotations

import concurrent.futures
import math
import random
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import cupy as np
except ImportError:
    import numpy as np
import pandas as pd

try:
    from deap import base, creator, tools
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError("StrategyOptimizer requires `deap`. Install via `pip install deap`.") from exc

try:
    import optuna
    from optuna.samplers import TPESampler
    from optuna.storages import JournalStorage, JournalFileStorage
    try:
        from optuna.storages import RedisStorage
        REDIS_STORAGE_AVAILABLE = True
    except ImportError:
        REDIS_STORAGE_AVAILABLE = False
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "StrategyOptimizer requires `optuna`. Install via `pip install optuna`."
    ) from exc

from config.config import Config
from core.quant.backtest.unified_engine import UnifiedBacktestEngine
from data_svc.database.optimized_data_query import OptimizedStockDataQuery
from core.quant.strategies.strategy_factory import StrategyFactory

MIN_FITNESS_SCORE = -1e9

# 缓存警告状态，避免重复警告（性能优化，确保 <1ms）
# 使用 frozenset 提高查找性能，限制缓存大小避免内存泄漏
_metric_warning_cache: set = set()
_MAX_WARNING_CACHE_SIZE = 1000  # 限制缓存大小


def _warn_missing_field(field_name: str, context: str = "") -> None:
    """快速警告缺失字段（使用缓存避免重复警告，确保 <1ms 响应）"""
    # 快速路径：如果缓存已满，直接返回（避免字符串拼接和打印开销）
    if len(_metric_warning_cache) >= _MAX_WARNING_CACHE_SIZE:
        return
    
    # 使用简单的字符串拼接（比 f-string 稍快）
    cache_key = f"{field_name}:{context}" if context else field_name
    
    # 快速检查：使用 in 操作符（O(1) 平均情况）
    if cache_key not in _metric_warning_cache:
        _metric_warning_cache.add(cache_key)
        # 只在第一次警告时打印，后续直接返回（避免 I/O 开销）
        if len(_metric_warning_cache) <= 100:  # 只打印前100个警告，避免日志过多
            print(f"[WARN] 警告: metrics 中缺失字段 '{field_name}'" + (f" ({context})" if context else "") + "，使用默认值")


def _sanitize_score(value: Any) -> float:
    """Ensure the fitness score is a finite float within safe bounds."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return MIN_FITNESS_SCORE
    if not math.isfinite(score):
        return MIN_FITNESS_SCORE
    if score < MIN_FITNESS_SCORE:
        return MIN_FITNESS_SCORE
    return score


def _metric_from_config(metrics: Dict[str, Any], target: str) -> float:
    """Helper to extract and normalize metric values.
    
    字段缺失时直接警告并返回既定的假数据（默认值），响应时间 <1ms。
    """
    if not metrics:
        return MIN_FITNESS_SCORE

    target = target.lower()
    
    # 快速检查字段是否存在（使用 in 操作符，确保 <1ms）
    field_mapping = {
        "sharpe": ("sharpeRatio", MIN_FITNESS_SCORE),
        "returns": ("annualizedReturn", MIN_FITNESS_SCORE),
        "calmar": ("calmarRatio", MIN_FITNESS_SCORE),
        "sortino": ("sortinoRatio", MIN_FITNESS_SCORE),
        "total_return": ("totalReturn", MIN_FITNESS_SCORE),
    }
    
    field_name, default_value = field_mapping.get(target, ("sharpeRatio", MIN_FITNESS_SCORE))
    
    # 快速检查：字段是否存在且非 None
    if field_name not in metrics or metrics[field_name] is None:
        _warn_missing_field(field_name, f"target={target}")
        return default_value
    
    value = metrics[field_name]
    return _sanitize_score(value)


def _get_metric_value(metrics: Optional[Dict[str, Any]], keys: Iterable[str], default: float = 0.0) -> float:
    """
    获取指标值，字段缺失时直接警告并返回既定的假数据（默认值），响应时间 <1ms。
    """
    if not metrics:
        return default
    
    # 快速检查：遍历 keys，找到第一个存在的字段（使用 in 操作符，确保 <1ms）
    for key in keys:
        if key in metrics and metrics[key] is not None:
            try:
                return float(metrics[key])
            except (TypeError, ValueError):
                continue
    
    # 所有字段都缺失，警告并返回默认值
    keys_str = ", ".join(keys)
    _warn_missing_field(keys_str, "get_metric_value")
    return default


def _trend_score(metrics: Optional[Dict[str, Any]]) -> float:
    if not metrics:
        return MIN_FITNESS_SCORE
    ret = _get_metric_value(
        metrics,
        ["annualizedReturn", "annualized_return", "totalReturn", "total_return"],
        default=0.0,
    )
    max_dd = abs(_get_metric_value(metrics, ["maxDrawdown", "max_drawdown"], default=0.0))
    score = ret - max_dd * 0.5
    return _sanitize_score(score)


def _calmar_score(metrics: Optional[Dict[str, Any]]) -> float:
    if not metrics:
        return MIN_FITNESS_SCORE
    ret = _get_metric_value(
        metrics,
        ["annualizedReturn", "annualized_return", "totalReturn", "total_return"],
        default=0.0,
    )
    max_dd = abs(_get_metric_value(metrics, ["maxDrawdown", "max_drawdown"], default=0.0))
    if max_dd <= 0:
        return _sanitize_score(ret)
    return _sanitize_score(ret / max_dd)


def _sharpe_score(metrics: Optional[Dict[str, Any]]) -> float:
    if not metrics:
        return MIN_FITNESS_SCORE
    return _sanitize_score(
        _get_metric_value(metrics, ["sharpeRatio", "sharpe_ratio"], default=0.0)
    )


def _rr_risk_score(metrics: Optional[Dict[str, Any]]) -> float:
    if not metrics:
        return MIN_FITNESS_SCORE
    pf = _get_metric_value(metrics, ["profitFactor", "profit_factor"], default=1.0)
    max_dd = abs(_get_metric_value(metrics, ["maxDrawdown", "max_drawdown"], default=0.0))
    score = pf / (1.0 + max_dd / 100.0)
    return _sanitize_score(score)


def compute_objective_scores(
    name: str,
    train_metrics: Optional[Dict[str, Any]],
    val_metrics: Optional[Dict[str, Any]] = None,
    test_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    objective = (name or "robust_trend_score").lower()
    train_score = _sharpe_score(train_metrics)
    val_score = _sharpe_score(val_metrics) if val_metrics else train_score
    test_score = _sharpe_score(test_metrics) if test_metrics else val_score

    if objective == "calmar":
        train_score = _calmar_score(train_metrics)
        val_score = _calmar_score(val_metrics) if val_metrics else train_score
        test_score = _calmar_score(test_metrics) if test_metrics else val_score
    elif objective == "sharpe":
        train_score = _sharpe_score(train_metrics)
        val_score = _sharpe_score(val_metrics) if val_metrics else train_score
        test_score = _sharpe_score(test_metrics) if test_metrics else val_score
    elif objective == "rr_risk_score":
        train_score = _rr_risk_score(train_metrics)
        val_score = _rr_risk_score(val_metrics) if val_metrics else train_score
        test_score = _rr_risk_score(test_metrics) if test_metrics else val_score
    elif objective in ("robust_trend_score", "multi_period_robust"):
        t = _trend_score(train_metrics)
        v = _trend_score(val_metrics) if val_metrics else t
        u = _trend_score(test_metrics) if test_metrics else v
        train_score, val_score, test_score = t, v, u

        if objective == "multi_period_robust":
            series: List[float] = []
            for s in (t, v, u):
                if s > MIN_FITNESS_SCORE / 2:
                    series.append(s)
            if series:
                mean_val = float(np.mean(series))  # type: ignore[arg-type]
                var_val = float(np.var(series))  # type: ignore[arg-type]
                mean_adj = _sanitize_score(mean_val - var_val)
                train_score = mean_adj
                val_score = mean_adj
                test_score = mean_adj

    score_gap = abs(val_score - train_score)
    return {
        "score_train": train_score,
        "score_val": val_score,
        "score_gap": score_gap,
        "score_test": test_score,
    }


def _build_strategy_params(param_vector: Iterable[float], param_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert GA chromosome / BO params into strategy keyword arguments."""
    params: Dict[str, Any] = {}
    for gene, spec in zip(param_vector, param_specs):
        name = spec["name"]
        ptype = spec.get("type", "float")
        if ptype == "int":
            params[name] = int(round(gene))
        elif ptype == "bool":
            params[name] = bool(round(gene))
        else:
            params[name] = float(gene)
    return params


def _evaluate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Standalone worker function executed inside worker processes.
    The payload must contain all serializable objects.
    
    Supports merging default parameters when only selected params are optimized.
    
    【健壮性加固】在最外层包裹异常捕获，确保任何错误都不会导致进程崩溃。
    """
    import os
    import traceback
    
    # 【最外层异常捕获】捕获所有可能的异常（内存错误、数据库锁、逻辑 Bug 等）
    try:
        strategy_name: str = payload.get("strategy_name", "")
        params_config: List[Dict[str, Any]] = payload.get("param_specs", [])
        genes: Iterable[float] = payload.get("genes", [])
        target_metric: str = payload.get("target_metric", "sharpe")
        start_date: str = payload.get("start_date", "")
        end_date: str = payload.get("end_date", "")
        db_path: str = payload.get("db_path", "")
        default_params: Optional[Dict[str, Any]] = payload.get("default_params")

        # 参数验证
        if not strategy_name or not start_date or not end_date:
            print(f"[WORKER {os.getpid()}] 参数不完整，返回最低分")
            return {
                "score": MIN_FITNESS_SCORE,
                "metrics": None,
                "params": {},
            }

        # 【关键修复】在并行环境中，每个进程必须创建独立的数据库连接
        # 禁用 warmup 避免重复初始化，在子进程中手动控制
        local_query = None
        try:
            # 创建数据查询实例（每个子进程独立实例，禁用 warmup）
            local_query = OptimizedStockDataQuery(db_path=db_path, warmup=False)
            
            # 【关键修复】在子进程中预加载数据，避免并行执行时数据未加载的问题
            # 检查是否禁用预加载（某些场景可能不需要）
            if os.getenv("DISABLE_PRELOAD", "0") != "1":
                # 计算需要预加载的日期范围（包含预热期）
                # 默认预热60天，覆盖MA60等指标
                from datetime import timedelta
                import pandas as pd
                required_warmup = 60  # 默认60天预热期
                try:
                    load_start_date = pd.to_datetime(start_date) - timedelta(days=required_warmup)
                    load_start_date_str = load_start_date.strftime('%Y-%m-%d')
                    
                    try:
                        local_query.preload_backtest_data(load_start_date_str, end_date)
                    except Exception as preload_err:
                        # 预加载失败不影响执行，会回退到逐日查询
                        print(f"[WORKER {os.getpid()}] 预加载数据失败，将使用逐日查询: {preload_err}")
                except Exception as date_err:
                    print(f"[WORKER {os.getpid()}] 日期解析失败，跳过预加载: {date_err}")
            
            optimizer = _EvaluationHelper(local_query)
            params = _build_strategy_params(genes, params_config)
            # 如果有默认参数，合并它们（优化参数优先）
            if default_params:
                merged_params = {**default_params, **params}
                params = merged_params
            evaluation = optimizer.evaluate(strategy_name, params, start_date, end_date, target_metric)
            return {
                "score": evaluation.get("score", MIN_FITNESS_SCORE),
                "metrics": evaluation.get("metrics"),
                "params": params,
            }
        except Exception as eval_err:
            # 评估过程中的错误，返回最低分
            print(f"[WORKER {os.getpid()}] 评估过程出错: {eval_err}")
            traceback.print_exc()
            return {
                "score": MIN_FITNESS_SCORE,
                "metrics": None,
                "params": {},
            }
        finally:
            # 【关键修复】确保清理资源，释放内存，避免内存泄漏
            if local_query is not None:
                try:
                    # 使用统一的 close() 方法清理所有资源
                    local_query.close()
                except Exception as e:
                    # 即使关闭失败也不应该影响主流程
                    print(f"[WORKER {os.getpid()}] 清理资源时出错（已忽略）: {e}")
                finally:
                    # 清理引用，帮助垃圾回收
                    local_query = None
                    
    except Exception as outer_err:
        # 【最外层异常捕获】捕获所有未预期的异常（内存错误、数据库锁、系统错误等）
        print(f"[WORKER {os.getpid()}] 【严重错误】Trial 崩溃，返回最低分: {outer_err}")
        traceback.print_exc()
        return {
            "score": MIN_FITNESS_SCORE,
            "metrics": None,
            "params": {},
        }


class _EvaluationHelper:
    """
    Lightweight helper that encapsulates evaluation logic.
    This class is deliberately simple so it can be used both in-process and in worker processes.
    """

    def __init__(self, data_query: OptimizedStockDataQuery):
        self.data_query = data_query

    def evaluate(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
        target_metric: str,
        stop_event=None,
    ) -> Dict[str, Any]:
        # 防止参数中包含 strategy_name/use_simple 等与工厂签名冲突的字段
        clean_params = dict(params)
        clean_params.pop("strategy_name", None)
        clean_params.pop("use_simple", None)

        try:
            strategy = StrategyFactory.create_strategy(strategy_name, **clean_params)
        except Exception:
            traceback.print_exc()
            return {"score": MIN_FITNESS_SCORE, "metrics": None}

        engine = UnifiedBacktestEngine(self.data_query)
        final_metric: Optional[Dict[str, Any]] = None
        try:
            stream = engine.run_backtest_streaming(start_date, end_date, strategy, stop_event=stop_event)
            for event in stream:
                if event.get("type") == "final_metrics":
                    final_metric = event.get("data")
                    break
        except Exception:
            traceback.print_exc()
            return {"score": MIN_FITNESS_SCORE, "metrics": None}

        if not final_metric:
            return {"score": MIN_FITNESS_SCORE, "metrics": None}
        score = _metric_from_config(final_metric, target_metric)
        return {"score": score, "metrics": final_metric}


class StrategyOptimizer:
    """
    Entry point for automated parameter optimization.

    Attributes:
        data_query: OptimizedStockDataQuery instance (shared for in-process evaluation).
        socketio: Optional socket server used to push live updates to the UI.
        logger: Optional logger.
        stop_event: Optional threading.Event to signal optimization stop.
    """

    def __init__(self, data_query: OptimizedStockDataQuery, socketio=None, logger=None, sid=None, stop_event=None):
        self.data_query = data_query
        self.socketio = socketio
        self.logger = logger
        self.sid = sid  # Socket.IO session ID for targeted emits
        self.stop_event = stop_event  # 停止事件
        self._evaluation_helper = _EvaluationHelper(self.data_query)
        # 评估计数器：用于向前端推送“最近评估结果”的序号
        self._evaluation_counter: int = 0

    # ---------------------------------------------------------------------- #
    # Core evaluation
    # ---------------------------------------------------------------------- #
    def _evaluate_strategy(
        self,
        strategy_name: str,
        params: Dict[str, Any],
        start_date: str,
        end_date: str,
        target_metric: str,
    ) -> Dict[str, Any]:
        return self._evaluation_helper.evaluate(strategy_name, params, start_date, end_date, target_metric, self.stop_event)

    # ------------------------------------------------------------------ #
    # Helper: 向前端推送单次评估结果（细粒度“最近评估结果”用）
    # ------------------------------------------------------------------ #
    def _emit_evaluation_event(self, evaluation: Dict[str, Any], context: Dict[str, Any]) -> None:
        """
        统一封装单次参数评估结果的推送逻辑。
        事件名：optimization_evaluation
        典型字段：
          - index: 第几次评估（全局递增）
          - score: 当前评估目标值（越大越好）
          - params: 当前评估使用的参数
          - metrics: 完整绩效指标（如果有）
          - algorithm / generation / iteration 等上下文字段
        """
        if not self.socketio:
            return

        try:
            self._evaluation_counter += 1
            score = evaluation.get("score")
            metrics = evaluation.get("metrics")
            # evaluation 里可能已经带有 params（GA 进程池返回），否则从 context 兜底
            params = evaluation.get("params") or context.get("params") or {}

            payload: Dict[str, Any] = {
                "index": self._evaluation_counter,
                "score": score,
                "params": params,
                "metrics": metrics,
            }
            payload.update(context or {})

            if self.sid:
                self.socketio.emit("optimization_evaluation", payload, to=self.sid)
            else:
                self.socketio.emit("optimization_evaluation", payload)
        except Exception as e:
            # 不要因为调试事件失败影响主流程
            print(f"[WARN] 发送 optimization_evaluation 事件失败: {e}")

    # ---------------------------------------------------------------------- #
    # Genetic Algorithm
    # ---------------------------------------------------------------------- #
    def run_genetic_optimization(
        self,
        strategy_name: str,
        start_date: str,
        end_date: str,
        param_specs: List[Dict[str, Any]],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Run genetic algorithm optimization.

        Args:
            strategy_name: strategy id
            start_date: backtest start
            end_date: backtest end
            param_specs: list of parameter configuration dicts
            options: GA related options (population, generations, mutation_rate, crossover_rate, target_metric)
        """
        population_size = options.get("population", 20)
        # 重要：严格使用前端传入的迭代次数
        iterations = options.get("iterations", 50)  # 从前端获取迭代次数
        generations = options.get("generations", iterations)  # 确保 generations 等于 iterations
        mutation_rate = options.get("mutation_rate", options.get("mutationRate", 0.1))
        crossover_rate = options.get("crossover_rate", options.get("crossoverRate", 0.7))
        target_metric = options.get("target_metric", options.get("target", "sharpe"))
        
        # 验证迭代次数设置
        print(f"🧬 遗传算法参数设置:")
        print(f"   前端传入 iterations: {options.get('iterations')}")
        print(f"   最终使用的 generations: {generations}")
        print(f"   实际循环代数: {generations}")

        self._register_deap_classes()
        toolbox = base.Toolbox()

        print(f"🧬 遗传算法参数设置:")
        for spec in param_specs:
            name = spec["name"]
            lower, upper = spec["bounds"]
            ptype = spec.get("type", "float")
            
            # 参数范围验证和修正
            if lower >= upper:
                print(f"[WARN]  参数范围验证失败: {name} 范围 [{lower}, {upper}] 无效，将使用安全范围")
                # 使用安全范围：如果下限太大，使用0到下限+100的范围
                if lower > 1000:
                    lower, upper = 0, lower + 100
                else:
                    # 否则交换上下限，确保下限小于上限
                    lower, upper = min(lower, upper - 1), max(upper, lower + 1)
                # 更新spec中的bounds
                spec["bounds"] = [lower, upper]
            
            print(f"   参数 {name}: [{lower}, {upper}] ({ptype})")
            
            if ptype == "int":
                toolbox.register(f"attr_{name}", random.randint, int(lower), int(upper))
            else:
                toolbox.register(f"attr_{name}", random.uniform, float(lower), float(upper))

        toolbox.register(
            "individual",
            tools.initCycle,
            creator.Individual,
            tuple(toolbox.__getattribute__(f"attr_{spec['name']}") for spec in param_specs),
            n=1,
        )
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)
        toolbox.register("mate", tools.cxUniform, indpb=0.5)
        toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.2, indpb=0.2)
        toolbox.register("select", tools.selTournament, tournsize=3)

        population = toolbox.population(n=population_size)
        db_path = getattr(self.data_query, "db_path", Config.DB_PATH)
        best_overall: Dict[str, Any] = {
            "score": float("-inf"),
            "params": None,
            "metrics": None,
        }

        # 限制并发进程数以避免内存溢出
        # 根据内存情况设置：16GB内存建议最多4-6个并发进程
        # 每个进程需要加载数据库和回测数据，大约占用2-3GB内存
        import os
        cpu_count = os.cpu_count() or 4
        # 保守设置：最多使用CPU核心数的一半，但不超过6个进程
        max_workers = min(max(2, cpu_count // 2), 6)
        print(f"🧬 进程池配置: CPU核心数={cpu_count}, 最大并发进程数={max_workers}")

        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            # 添加硬性停止条件，确保绝对不会超过指定代数
            actual_generations = min(generations, options.get("iterations", 50))
            for generation in range(actual_generations):
                # 检查停止事件
                if self.stop_event and self.stop_event.is_set():
                    print(f"🛑 优化被用户中断 (sid: {self.sid})")
                    if self.socketio and self.sid:
                        self.socketio.emit("optimization_cancelled", {"message": "优化已停止"}, to=self.sid)
                    return {
                        "best_score": best_overall.get("score"),
                        "best_params": best_overall.get("params"),
                        "best_metrics": best_overall.get("metrics"),
                        "cancelled": True,
                    }
                # 检查当前代种群的多样性
                if generation % 5 == 0:  # 每5代打印一次种群信息
                    unique_params = set()
                    for ind in population:
                        param_values = []
                        for gene, spec in zip(ind, param_specs):
                            param_values.append(f"{spec['name']}={gene:.3f}")
                        unique_params.add(tuple(param_values))
                    print(f"   代 {generation}: 种群大小 {len(population)}, 独特参数组合 {len(unique_params)}")
                
                # 创建 future 到 individual 的映射，因为 Individual 对象不可哈希
                future_to_individual = {}
                futures = []
                for individual in population:
                    future = executor.submit(
                        _evaluate_payload,
                        {
                            "strategy_name": strategy_name,
                            "param_specs": param_specs,
                            "genes": individual,
                            "target_metric": target_metric,
                            "start_date": start_date,
                            "end_date": end_date,
                            "db_path": db_path,
                            "default_params": options.get("default_params"),  # 传递默认参数
                        },
                    )
                    future_to_individual[future] = individual
                    futures.append(future)
                
                # 使用 future 作为键来匹配 individual 和 fitness
                for future in concurrent.futures.as_completed(futures):
                    individual = future_to_individual[future]
                    evaluation_result = future.result()
                    fitness = evaluation_result.get("score", MIN_FITNESS_SCORE)
                    individual.fitness.values = (fitness,)
                    individual._evaluation_result = evaluation_result

                    # 细粒度：每评估一个个体就向前端推送一次 evaluation 事件
                    self._emit_evaluation_event(
                        evaluation_result,
                        {
                            "algorithm": "ga",
                            "phase": "population",
                            "generation": generation + 1,
                            "total_generations": actual_generations,
                        },
                    )

                best_individual = tools.selBest(population, 1)[0]
                best_value = best_individual.fitness.values[0]
                if best_value > best_overall["score"]:
                    best_overall = {
                        "score": best_value,
                        "params": _build_strategy_params(best_individual, param_specs),
                        "metrics": getattr(best_individual, "_evaluation_result", {}).get("metrics"),
                    }

                if self.socketio:
                    progress_pct = int((generation + 1) / actual_generations * 100)
                    
                    # 确保 best_overall[1] 是字典类型
                    best_params_dict = best_overall["params"] if best_overall["params"] else {}
                    if not isinstance(best_params_dict, dict):
                        print(f"[WARN]  警告: best_overall[1] 不是字典类型: {type(best_params_dict)}, 值: {best_params_dict}")
                        best_params_dict = {}
                    
                    emit_data = {
                        "progress": progress_pct,
                        "generation": generation + 1,
                        "total_generations": actual_generations,
                        "current_best": best_value,
                        "best_metric": best_value,
                        "params": best_params_dict,
                        "best_params": best_params_dict,
                        "metrics": best_overall.get("metrics"),
                    }
                    if self.sid:
                        self.socketio.emit("optimization_progress", emit_data, to=self.sid)
                    else:
                        self.socketio.emit("optimization_progress", emit_data)

                # 正确的遗传算法流程：
                # 1. 选择父代（从评估后的种群中选择）
                parents = toolbox.select(population, population_size)
                
                # 2. 克隆父代作为子代（准备进行交叉和变异）
                offspring = list(map(toolbox.clone, parents))

                # 3. 交叉操作：配对父代进行交叉
                for child1, child2 in zip(offspring[::2], offspring[1::2]):
                    if random.random() < crossover_rate:
                        toolbox.mate(child1, child2)
                        del child1.fitness.values
                        del child2.fitness.values

                # 4. 变异操作
                for mutant in offspring:
                    if random.random() < mutation_rate:
                        toolbox.mutate(mutant)
                        del mutant.fitness.values

                # 5. 评估新产生的子代（只评估fitness无效的个体）
                invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
                if invalid_ind:
                    future_to_individual = {}
                    futures = []
                    for individual in invalid_ind:
                        future = executor.submit(
                            _evaluate_payload,
                            {
                                "strategy_name": strategy_name,
                                "param_specs": param_specs,
                                "genes": individual,
                                "target_metric": target_metric,
                                "start_date": start_date,
                                "end_date": end_date,
                                "db_path": db_path,
                                "default_params": options.get("default_params"),
                            },
                        )
                        future_to_individual[future] = individual
                        futures.append(future)
                    
                    for future in concurrent.futures.as_completed(futures):
                        individual = future_to_individual[future]
                        evaluation_result = future.result()
                        fitness = evaluation_result.get("score", MIN_FITNESS_SCORE)
                        individual.fitness.values = (fitness,)
                        individual._evaluation_result = evaluation_result

                        # 细粒度：对子代评估同样推送 evaluation 事件
                        self._emit_evaluation_event(
                            evaluation_result,
                            {
                                "algorithm": "ga",
                                "phase": "offspring",
                                "generation": generation + 1,
                                "total_generations": actual_generations,
                            },
                        )

                # 6. 精英保留策略：保留当前代最优个体，替换种群
                # 使用 (μ+λ) 选择策略：从父代和子代中选择最优个体
                elite_size = max(1, int(population_size * 0.1))  # 保留10%的精英
                elite = tools.selBest(population, elite_size)
                offspring.extend(elite)
                population[:] = tools.selBest(offspring, population_size)

        return {
            "best_score": best_overall.get("score"),
            "best_params": best_overall.get("params"),
            "best_metrics": best_overall.get("metrics"),
        }

    # ---------------------------------------------------------------------- #
    # Bayesian Optimization (using Optuna with TPE sampler)
    # ---------------------------------------------------------------------- #
    def run_bayesian_optimization(
        self,
        strategy_name: str,
        start_date: str,
        end_date: str,
        param_specs: List[Dict[str, Any]],
        options: Dict[str, Any],
    ) -> Dict[str, Any]:
        target_metric = options.get("target_metric", options.get("target", "sharpe"))
        init_points = options.get("init_points", 3)
        # 重要：严格使用前端传入的迭代次数
        iterations = options.get("iterations", 20)  # 从前端获取迭代次数
        
        # 验证贝叶斯优化迭代次数设置
        print(f"🎯 Optuna 贝叶斯优化参数设置:")
        print(f"   前端传入 iterations: {options.get('iterations')}")
        print(f"   实际迭代次数: {iterations}")
        print(f"   初始随机采样点数: {init_points}")

        # 参数范围验证和修正
        validated_param_specs = []
        for spec in param_specs:
            name = spec["name"]
            bounds = spec["bounds"]
            lower, upper = bounds
            
            # 参数范围验证和修正
            if lower >= upper:
                print(f"[WARN]  贝叶斯优化参数范围验证失败: {name} 范围 [{lower}, {upper}] 无效，将使用安全范围")
                # 使用安全范围：如果下限太大，使用0到下限+100的范围
                if lower > 1000:
                    lower, upper = 0, lower + 100
                else:
                    # 否则交换上下限，确保下限小于上限
                    lower, upper = min(lower, upper - 1), max(upper, lower + 1)
                # 更新spec中的bounds
                spec["bounds"] = [lower, upper]
            
            validated_param_specs.append(spec)
        
        param_specs = validated_param_specs
        default_params = options.get("default_params", {})
        db_path = getattr(self.data_query, "db_path", Config.DB_PATH)
        
        # 设置并行进程数
        import os
        cpu_count = os.cpu_count() or 4
        # 使用所有 CPU 核心进行并行优化
        n_jobs = 5 # -1 表示使用所有可用 CPU 核心
        print(f"🎯 Optuna 并行配置: CPU核心数={cpu_count}, 并行任务数={n_jobs} (使用所有核心)")
        
        # 配置 Optuna Storage（优先使用 Redis，支持分布式并行，无锁冲突）
        import os
        import time
        
        # 从环境变量读取 Redis Storage URL，默认为 redis://localhost:6379/0
        redis_storage_url = os.getenv("OPTUNA_REDIS_STORAGE_URL", "redis://localhost:6379/0")
        
        # 使用时间戳确保 study_name 唯一性
        timestamp = int(time.time() * 1000)
        study_name = f"optuna_{strategy_name}_{start_date}_{end_date}_{timestamp}"
        sampler = TPESampler(seed=options.get("random_state", 48))
        
        study = None
        storage_used = None
        
        # 优先尝试 Redis Storage（支持分布式并行，无锁冲突）
        if REDIS_STORAGE_AVAILABLE and redis_storage_url.startswith("redis://"):
            try:
                redis_storage = RedisStorage(url=redis_storage_url)
                study = optuna.create_study(
                    study_name=study_name,
                    storage=redis_storage,
                    sampler=sampler,
                    direction="maximize",
                    load_if_exists=True,
                )
                storage_used = "Redis"
                print(f"✅ 使用 Redis Storage: {redis_storage_url}")
            except Exception as e:
                print(f"[WARN] 创建 Optuna Redis Storage 失败: {e}")
                import traceback
                traceback.print_exc()
        
        # 如果 Redis 失败，回退到 JournalStorage（文件系统，支持并行）
        if study is None:
            try:
                journal_dir = os.path.join(os.getcwd(), "optuna_logs")
                os.makedirs(journal_dir, exist_ok=True)
                import tempfile
                storage_dir = os.path.join(os.path.dirname(db_path) if db_path else tempfile.gettempdir(), "optuna_storage")
                os.makedirs(storage_dir, exist_ok=True)
                journal_path = os.path.join(storage_dir, f"optuna_{strategy_name}_{start_date}_{end_date}_{timestamp}.log")
                journal_storage = JournalStorage(JournalFileStorage(journal_path))
                study = optuna.create_study(
                    study_name=study_name,
                    storage=journal_storage,
                    sampler=sampler,
                    direction="maximize",
                    load_if_exists=True,
                )
                storage_used = "JournalStorage"
                print(f"✅ 使用 JournalStorage 文件存储: {journal_path}")
            except Exception as e:
                print(f"[WARN] 创建 Optuna JournalStorage 失败，尝试使用内存存储: {e}")
                import traceback
                traceback.print_exc()
                # 如果文件存储失败，回退到内存存储（不支持并行）
                study = optuna.create_study(
                    study_name=study_name,
                    sampler=sampler,
                    direction="maximize",
                )
                n_jobs = 1  # 内存存储不支持并行
                storage_used = "InMemory"
                print(f"[WARN] 回退到内存存储，并行已禁用 (n_jobs=1)")
        
        # 评估计数器（用于进度追踪）
        evaluation_count = [0]  # 使用列表以便在闭包中修改
        
        # 定义目标函数（接收 trial 对象）
        def objective(trial: optuna.Trial) -> float:
            """Optuna 目标函数：根据 trial 对象建议参数并评估策略"""
            # 检查停止事件（在每个 trial 开始时检查）
            if self.stop_event and self.stop_event.is_set():
                raise optuna.TrialPruned("优化被用户中断")
            
            # 根据参数规格使用 trial.suggest_* 方法定义参数空间
            params = {}
            for spec in param_specs:
                name = spec["name"]
                lower, upper = spec["bounds"]
                ptype = spec.get("type", "float")
                
                if ptype == "int":
                    params[name] = trial.suggest_int(name, int(lower), int(upper))
                elif ptype == "bool":
                    params[name] = trial.suggest_categorical(name, [True, False])
                else:
                    # float 类型，检查是否需要 log 尺度
                    log = spec.get("log", False)
                    params[name] = trial.suggest_float(name, float(lower), float(upper), log=log)
            
            # 合并默认参数（优化参数优先）
            merged_params = {**default_params, **params}
            
            # 【关键修复】在并行环境中，每个 trial 需要创建独立的数据查询对象
            # 不能使用 self._evaluate_strategy（它使用主进程的 data_query，可能包含不可序列化的连接）
            # 使用 _EvaluationHelper 创建独立的评估实例
            local_query = None
            try:
                # 创建独立的数据查询实例（每个 trial 独立，避免共享连接）
                local_query = OptimizedStockDataQuery(db_path=db_path, warmup=False)
                
                # 预加载数据（如果需要）
                import os
                if os.getenv("DISABLE_PRELOAD", "0") != "1":
                    try:
                        from datetime import timedelta
                        import pandas as pd
                        required_warmup = 60
                        load_start_date = pd.to_datetime(start_date) - timedelta(days=required_warmup)
                        load_start_date_str = load_start_date.strftime('%Y-%m-%d')
                        local_query.preload_backtest_data(load_start_date_str, end_date)
                    except Exception:
                        pass  # 预加载失败不影响执行
                
                # 创建独立的评估助手
                optimizer = _EvaluationHelper(local_query)
                evaluation = optimizer.evaluate(strategy_name, merged_params, start_date, end_date, target_metric)
                score = evaluation.get("score", MIN_FITNESS_SCORE)
            finally:
                # 确保清理资源
                if local_query is not None:
                    try:
                        local_query.close()
                    except:
                        pass
                    local_query = None
            
            # 更新评估计数器
            evaluation_count[0] += 1
            current_count = evaluation_count[0]
            total_trials = init_points + iterations
            
            # 细粒度：每评估一次就推送 evaluation 事件
            try:
                self._emit_evaluation_event(
                    {
                        "score": score,
                        "metrics": evaluation.get("metrics"),
                        "params": merged_params,
                    },
                    {
                        "algorithm": "bayesian",
                        "trial": current_count,
                        "total_trials": total_trials,
                    },
                )
            except Exception as e:
                print(f"[WARN] Optuna evaluation 事件发送失败: {e}")
            
            # 向前端推送进度更新
            if self.socketio:
                try:
                    progress_pct = int((current_count / total_trials) * 100) if total_trials > 0 else 0
                    best_trial = study.best_trial if study.trials else None
                    
                    formatted_params = merged_params.copy()
                    best_score = best_trial.value if best_trial else score
                    
                    emit_data = {
                        "progress": progress_pct,
                        "iteration": current_count,
                        "total_iterations": total_trials,
                        "current_best": best_score,
                        "best_metric": best_score,
                        "params": formatted_params,
                        "best_params": formatted_params,
                        "metrics": evaluation.get("metrics"),
                    }
                    if self.sid:
                        self.socketio.emit("optimization_progress", emit_data, to=self.sid)
                    else:
                        self.socketio.emit("optimization_progress", emit_data)
                except Exception as e:
                    print(f"[WARN] 进度推送失败: {e}")
            
            # 打印日志
            print(f"  Trial {current_count}/{total_trials}: score={score:.4f}, params={params}")
            
            return score
        
        # 执行优化（并行）
        print(f"🚀 开始 Optuna 优化（TPE 采样器，并行执行）...")
        try:
            # 使用 study.optimize() 执行优化，n_jobs=-1 启用并行
            study.optimize(
                objective,
                n_trials=init_points + iterations,  # 总试验次数 = 初始点数 + 迭代次数
                n_jobs=n_jobs,  # 并行任务数（-1 表示使用所有 CPU 核心）
                show_progress_bar=False,  # 我们使用自定义的进度推送
            )
        except KeyboardInterrupt:
            print(f"🛑 Optuna 优化被用户中断")
            if self.socketio and self.sid:
                self.socketio.emit("optimization_cancelled", {"message": "优化已停止"}, to=self.sid)
            # 即使中断，也返回当前最佳结果
        except optuna.TrialPruned:
            # Trial 被中断（通过 stop_event）
            print(f"🛑 Optuna 优化被用户中断（通过 stop_event）")
            if self.socketio and self.sid:
                self.socketio.emit("optimization_cancelled", {"message": "优化已停止"}, to=self.sid)
        except Exception as e:
            print(f"[WARN] Optuna 优化出错: {e}")
            import traceback
            traceback.print_exc()
            if self.socketio and self.sid:
                self.socketio.emit("optimization_error", {"message": str(e)}, to=self.sid)
        
        # 检查停止事件
        cancelled = False
        if self.stop_event and self.stop_event.is_set():
            cancelled = True
            print(f"🛑 Optuna 优化被用户中断 (sid: {self.sid})")
            if self.socketio and self.sid:
                self.socketio.emit("optimization_cancelled", {"message": "优化已停止"}, to=self.sid)
        
        # 获取最佳结果
        if not study.trials:
            print("[WARN] 没有完成任何 trial，返回默认结果")
            return {
                "best_score": MIN_FITNESS_SCORE,
                "best_params": {},
                "best_metrics": None,
                "cancelled": cancelled,
            }
        
        best_trial = study.best_trial
        best_params_raw = best_trial.params.copy()
        best_score = best_trial.value
        
        # 确保参数类型与原始 spec 一致
        best_params = {}
        for spec in param_specs:
            name = spec["name"]
            if name not in best_params_raw:
                continue
            ptype = spec.get("type", "float")
            value = best_params_raw[name]
            
            if ptype == "int":
                best_params[name] = int(value)
            elif ptype == "bool":
                best_params[name] = bool(value)
            else:
                best_params[name] = float(value)
        
        # 合并默认参数
        merged_best = {**default_params, **best_params}
        
        # 获取最佳结果的完整评估（包含 metrics）
        best_eval = self._evaluate_strategy(strategy_name, merged_best, start_date, end_date, target_metric)
        
        print(f"✅ Optuna 优化完成:")
        print(f"   最佳得分: {best_score:.4f}")
        print(f"   最佳参数: {best_params}")
        print(f"   总试验次数: {len(study.trials)}")
        if cancelled:
            print(f"   状态: 已中断")
        
        return {
            "best_score": best_eval.get("score", best_score),
            "best_params": best_params,
            "best_metrics": best_eval.get("metrics"),
            "cancelled": cancelled,
        }

    # ---------------------------------------------------------------------- #
    # Helper: Standardize metrics for frontend
    # ---------------------------------------------------------------------- #
    def _standardize_metrics(self, raw_metrics: Optional[Dict[str, Any]], score: float = 0.0) -> Dict[str, Any]:
        """
        Convert backend metrics to frontend standard format.
        frontend expects: annual_return, max_drawdown, profit_factor, sharpe, composite_score
        
        字段缺失时直接警告并返回既定的假数据（默认值），响应时间 <1ms。
        """
        if not raw_metrics:
            return {
                "annual_return": 0.0,
                "max_drawdown": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "composite_score": score,
                "score": score,
            }
        
        # 快速检查字段是否存在（使用 in 操作符，确保 <1ms）
        field_mappings = [
            ("annualizedReturn", "annual_return", 0.0),
            ("maxDrawdown", "max_drawdown", 0.0),
            ("profitFactor", "profit_factor", 0.0),
            ("sharpeRatio", "sharpe", 0.0),
        ]
        
        result = {
            "composite_score": score,
            "score": score,
            # Preserve original keys just in case
            **raw_metrics
        }
        
        # 快速检查并填充字段（缺失时警告并使用默认值）
        for source_field, target_field, default_value in field_mappings:
            if source_field not in raw_metrics or raw_metrics[source_field] is None:
                _warn_missing_field(source_field, "standardize_metrics")
                result[target_field] = default_value
            else:
                result[target_field] = raw_metrics[source_field]
        
        return result

    # ---------------------------------------------------------------------- #
    # Unified entry
    # ---------------------------------------------------------------------- #
    def run_optimization(self, config: Dict[str, Any]) -> Dict[str, Any]:
        method = config.get("method", "genetic")
        strategy_name = config["strategy_name"]
        start_date = config["start_date"]
        end_date = config["end_date"]
        param_specs = config["param_ranges"]
        options = config.get("options", {})
        selected_params = config.get("selected_params", None)  # 新增：选定的参数列表

        # 三段区间与模式/目标
        mode = config.get("mode", options.get("mode", "robust"))
        train_start = config.get("train_start_date", start_date)
        train_end = config.get("train_end_date", end_date)
        val_start = config.get("val_start_date")
        val_end = config.get("val_end_date")
        test_start = config.get("test_start_date")
        test_end = config.get("test_end_date")
        objective_name = options.get("objective", "robust_trend_score")

        # quick_explore 模式：限制迭代次数
        if mode == "quick_explore":
            max_iters = min(20, int(options.get("iterations", 20)))
            options["iterations"] = max_iters
            if method == "genetic":
                options["generations"] = max_iters

        # 每次新的优化任务开始时重置评估计数器
        self._evaluation_counter = 0

        # 发送开始事件
        if self.socketio:
            emit_data = {
                "strategy_name": strategy_name,
                "method": method,
                "iterations": options.get("iterations", options.get("generations", 50)),
                "param_count": len(param_specs),
                "mode": mode,
                "objective": objective_name,
            }
            if self.sid:
                self.socketio.emit("optimization_start", emit_data, to=self.sid)
            else:
                self.socketio.emit("optimization_start", emit_data)

        # 如果指定了 selected_params，只优化这些参数
        if selected_params is not None and len(selected_params) > 0:
            default_strategy = StrategyFactory.create_strategy(strategy_name)
            default_params = self._get_default_params(default_strategy, allowed_params=selected_params)

            filtered_param_specs = [
                spec for spec in param_specs
                if spec.get("name") in selected_params
            ]

            if not filtered_param_specs:
                raise ValueError(
                    f"选定的参数 {selected_params} 在 param_ranges 中未找到。"
                    f"可用参数: {[s.get('name') for s in param_specs]}"
                )

            options["default_params"] = default_params
            options["selected_params"] = selected_params
            param_specs = filtered_param_specs

        # 重要：确保遗传算法的 generations 与前端的 iterations 保持一致
        if method == "genetic":
            options["generations"] = options.get("iterations", options.get("generations", 50))
            print(f"🔧 遗传算法设置: {options['generations']} 代 (来自 iterations: {options.get('iterations')})")

        try:
            if method == "bayesian":
                result = self.run_bayesian_optimization(
                    strategy_name, train_start, train_end, param_specs, options
                )
            else:
                result = self.run_genetic_optimization(
                    strategy_name, train_start, train_end, param_specs, options
                )
        finally:
            try:
                self.data_query.clear_preloaded_data()
            except Exception:
                pass

        # 训练期最佳结果
        best_score = result.get("best_score")
        best_params = result.get("best_params", {}) or {}
        train_metrics_raw = result.get("best_metrics")

        # 在验证/测试区间上评估最终选择的参数
        # CHANGED: 只要区间有效，就计算 metrics，不再受限于 mode
        val_metrics_raw = None
        test_metrics_raw = None

        target_metric = options.get("target", options.get("target_metric", "sharpe"))

        if best_params:
            # 为避免过严的区间判断，只要 start/end 同时存在且 start < end 就尝试评估，
            # 其余的日期合法性（是否与数据库有交集）交给回测引擎自身处理。
            if val_start and val_end and str(val_start) < str(val_end):
                eval_val = self._evaluate_strategy(
                    strategy_name,
                    best_params,
                    val_start,
                    val_end,
                    target_metric,
                )
                val_metrics_raw = eval_val.get("metrics")
            else:
                val_start = None
                val_end = None

            if test_start and test_end and str(test_start) < str(test_end):
                eval_test = self._evaluate_strategy(
                    strategy_name,
                    best_params,
                    test_start,
                    test_end,
                    target_metric,
                )
                test_metrics_raw = eval_test.get("metrics")
            else:
                test_start = None
                test_end = None

        scores = compute_objective_scores(objective_name, train_metrics_raw, val_metrics_raw, test_metrics_raw)
        
        # Standardize metrics
        train_metrics = self._standardize_metrics(train_metrics_raw, scores.get("score_train", MIN_FITNESS_SCORE))
        val_metrics = self._standardize_metrics(val_metrics_raw, scores.get("score_val", MIN_FITNESS_SCORE)) if val_metrics_raw else None
        test_metrics = self._standardize_metrics(test_metrics_raw, scores.get("score_test", MIN_FITNESS_SCORE)) if test_metrics_raw else None

        # 统一从 options 提取总迭代次数 / 代数
        total_iters = options.get("iterations") or options.get("generations") or 0

        # 调试日志：打印最终选择的区间与指标，便于排查 val/test 为空的原因
        try:
            print("📌 StrategyOptimizer.run_optimization 最终区间与指标:")
            print(f"   strategy = {strategy_name}, method = {method}, mode = {mode}, objective = {objective_name}")
            print(f"   train_range = {train_start} ~ {train_end}")
            print(f"   val_range   = {val_start} ~ {val_end}")
            print(f"   test_range  = {test_start} ~ {test_end}")
            print(f"   train_metrics = {train_metrics}")
            print(f"   val_metrics   = {val_metrics}")
            print(f"   test_metrics  = {test_metrics}")
        except Exception:
            # 日志失败不能影响主流程
            pass

        # 构建 candidates / final_selected 结构（目前以单一最优解为候选）
        candidate = {
            "run_id": "best",
            "params": best_params,
            "score_train": scores.get("score_train", MIN_FITNESS_SCORE),
            "score_val": scores.get("score_val", MIN_FITNESS_SCORE),
            "score_gap": scores.get("score_gap", 0.0),
            "score_test": scores.get("score_test", MIN_FITNESS_SCORE), # Added score_test
            "train_metrics": train_metrics,
            "val_metrics": val_metrics,
            "test_metrics": test_metrics,
        }
        final_selected = {
            "run_id": "best",
            "params": best_params,
            "score_train": scores.get("score_train", MIN_FITNESS_SCORE),
            "score_val": scores.get("score_val", MIN_FITNESS_SCORE),
            "score_gap": scores.get("score_gap", 0.0),
            "score_test": scores.get("score_test", MIN_FITNESS_SCORE), # Added score_test
            "train_metrics": train_metrics,
            "val_metrics": val_metrics,
            "test_metrics": test_metrics,
        }

        result["candidates"] = [candidate]
        result["final_selected"] = final_selected

        if self.socketio:
            best_metrics = train_metrics

            try:
                progress_payload = {
                    "progress": 100,
                    "iteration": total_iters or None,
                    "total_iterations": total_iters or None,
                    "current_best": best_score,
                    "best_metric": best_score,
                    "params": best_params,
                    "best_params": best_params,
                    "metrics": best_metrics,
                }
                if self.sid:
                    self.socketio.emit("optimization_progress", progress_payload, to=self.sid)
                else:
                    self.socketio.emit("optimization_progress", progress_payload)
            except Exception as e:
                print(f"[WARN] 发送最终 optimization_progress 事件失败: {e}")

            emit_data = {
                "best_score": best_score,
                "best_params": best_params,
                "best_metrics": best_metrics,
                "method": method,
                "total_iterations": total_iters or None,
                "mode": mode,
                "objective": objective_name,
                "candidates": result.get("candidates"),
                "final_selected": result.get("final_selected"),
            }
            if self.sid:
                self.socketio.emit("optimization_complete", emit_data, to=self.sid)
            else:
                self.socketio.emit("optimization_complete", emit_data)
        return result

    def _get_default_params(self, strategy_instance, allowed_params: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """
        从策略实例中提取默认参数值。
        
        支持策略使用 dataclass 配置（如 SimpleVolumeConfig）或直接属性。
        """
        default_params: Dict[str, Any] = {}
        allowed_set = set(allowed_params or [])
        
        # 尝试从 config 属性获取（如 SimpleVolumeStrategyV3）
        if hasattr(strategy_instance, "config"):
            config = strategy_instance.config
            # 如果是 dataclass，使用 fields() 获取所有字段
            try:
                from dataclasses import fields, asdict
                if hasattr(config, "__dataclass_fields__"):
                    default_params = asdict(config)
                else:
                    # 普通对象，尝试获取所有属性
                    default_params = {
                        k: getattr(config, k)
                        for k in dir(config)
                        if not k.startswith("_") and not callable(getattr(config, k))
                    }
            except Exception:
                # 如果失败，尝试直接访问常见属性
                if hasattr(config, "__dict__"):
                    default_params = {
                        k: v for k, v in config.__dict__.items()
                        if not k.startswith("_")
                    }
        
        # 如果策略实例本身有参数属性，且明确允许，也添加进去
        if allowed_set:
            for attr_name in allowed_set:
                if attr_name in default_params:
                    continue
                if not hasattr(strategy_instance, attr_name):
                    continue
                try:
                    value = getattr(strategy_instance, attr_name)
                except Exception:
                    continue
                if isinstance(value, (int, float, str, bool)):
                    default_params[attr_name] = value
        
        return default_params

    # ------------------------------------------------------------------ #
    @staticmethod
    def _register_deap_classes():
        """Ensure DEAP creator classes are registered only once."""
        if not hasattr(creator, "FitnessMax"):
            creator.create("FitnessMax", base.Fitness, weights=(1.0,))
        if not hasattr(creator, "Individual"):
            creator.create("Individual", list, fitness=creator.FitnessMax)

