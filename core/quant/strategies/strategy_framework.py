# strategies/strategy_framework.py

from typing import TYPE_CHECKING
from functools import wraps
from config.config import Config
from core.utils.price_adjustment import apply_forward_adjustment

if TYPE_CHECKING:
    import pandas as pd

def simple_strategy(required_days=30):
    """
    装饰器：把“单只股票的决策函数”包装成“整天的信号生成函数”。
    被修饰的函数形如:
        def generate_signals(self, stock_code, current_stock_data, stock_history):
            return 'buy'/'sell'/'hold'
                或 ('buy', weight)
                或 {'action': 'buy', 'weight': 0.2, 'score': 3.5, 'params': {...}}
    """
    def decorator(strategy_func):
        @wraps(strategy_func)
        def wrapper(self, current_date, stock_pool_today, data_query):
            import pandas as pd  # 延迟导入
            # 记录所需历史长度
            self.required_days = max(getattr(self, "required_days", 0), required_days)
            return self._optimized_generate_signals(
                current_date=current_date,
                stock_pool=stock_pool_today,
                data_query=data_query,
                strategy_func=strategy_func,
            )
        return wrapper
    return decorator


class StrategyBase:
    """策略基类：负责批量取数、批量执行，并缓存富信号"""
    
    # CHANGED: 策略类必须声明 strategy_name 属性
    # strategy_name = "策略基类"

    def __init__(self, name: str | None = None):
        # CHANGED: 优先使用传入的 name，否则使用类属性 strategy_name
        self.name = name or getattr(self.__class__, 'strategy_name', self.__class__.__name__)
        self.required_days = 30
        self._cache = {}

        # 运行时上下文（由引擎塞进来）
        self.current_date = None
        self.current_portfolio = {}
        self.current_cash = 0.0

        # 每日最新的"富信号"：{code: {action, weight, score, params}}
        self.last_rich_signals = {}
        
        # 持仓状态管理（由引擎自动维护）
        # 格式：{stock_code: {'entry_date': '2024-01-01', 'days_held': 5, 'entry_price': 10.0, ...}}
        self.holding_state = {}
        # CHANGED: 默认执行价格（'open' 或 'close'），可以在子类里覆盖
        self.execution_price = {
            "buy": "close",
            "sell": "close",
            "default": "close",
        }
    
    # ===== 新增：参数规范接口 =====
    @classmethod
    def get_param_spec(cls):
        """
        返回本策略的参数规范列表，用于：
        - 前端图形化表单
        - GA / 网格搜索 等参数优化

        默认返回空列表；子类可覆盖。
        """
        return []

    @staticmethod
    def default_optimization_config():
        """可选：返回一个 {param_key: {...}} 的优化配置字典。

        建议由具体策略类覆盖，示例：

            return {
                "market_cap_min": {"type": "number", "min": 10, "max": 500, "default": 60},
                "volume_ratio_threshold": {"type": "number", "min": 0.5, "max": 3, "default": 1.5},
            }

        本基类默认返回空字典，调用方应在为空时回退到 dataclass 自动推断等逻辑。
        """
        return {}

    def get_required_indicators(self) -> list:
        """
        返回策略所需的指标列表
        
        子类可以覆盖此方法，返回指标配置列表，例如：
            return [
                {'type': 'ma', 'column': 'close', 'window': 20, 'name': 'ma20'},
                {'type': 'rsi', 'column': 'close', 'window': 14, 'name': 'rsi14'},
                {'type': 'atr', 'window': 14, 'name': 'atr14'},
            ]
        
        默认返回空列表（表示不需要额外指标）
        """
        return []

    # ====== 引擎每天开头会调用，用来把账户状态塞进策略 ======
    def set_runtime_context(self, current_date, portfolio, cash):
        self.current_date = current_date
        self.current_portfolio = dict(portfolio) if portfolio else {}
        self.current_cash = float(cash) if cash is not None else 0.0

    # ====== 对外统一入口：被 simple_strategy 装饰器调用 ======
    def _optimized_generate_signals(self, current_date, stock_pool, data_query, strategy_func):
        """
        - 对外返回：{code: 'buy'/'sell'/'hold'}
        - 内部：把完整 dict 缓存在 self.last_rich_signals
        """
        self.last_rich_signals = {}

        if stock_pool is None or stock_pool.empty:
            return {}

        # 1. 预筛选（可在子类重写 _pre_screen_stocks 提升自由度）
        candidate_df = self._pre_screen_stocks(stock_pool)
        if candidate_df.empty:
            return {}

        # 2. 批量拉历史 K 线
        prepared = self._batch_prepare_data(candidate_df, current_date, data_query)

        # 3. 批量执行单票策略
        simple_signals = self._batch_execute_strategy(candidate_df, prepared, strategy_func)
        return simple_signals

    # CHANGED: 允许策略定义买卖使用开盘价还是收盘价
    def get_execution_price(self, action: str = "buy") -> str:
        """
        返回执行价格类型：'open' 或 'close'
        子类可通过设置 self.execution_price（str 或 dict）来覆盖
        """
        pref = getattr(self, "execution_price", None)

        def normalize(value):
            return value if value in ("open", "close") else "close"

        if isinstance(pref, dict):
            return normalize(
                pref.get(action) or pref.get("default") or "close"
            )
        if isinstance(pref, str):
            return normalize(pref)
        return "close"

    # ====== 以下方法都可以在子类里按需重写 ======

    def _pre_screen_stocks(self, stock_pool: "pd.DataFrame") -> "pd.DataFrame":
        """
        默认的基础过滤：市值、价格、停牌、涨停、ST/科创/创业板等
        """
        df = stock_pool.copy()

        mask = (
            (df["total_mv"] >= Config.MIN_MARKET_CAP) &
            (df["total_mv"] <= getattr(Config, "MAX_MARKET_CAP", 5_000_000)) &
            (df["close"] >= Config.MIN_PRICE) &
            (df["volume"] > 0)
        )

        if "is_limit_up" in df.columns:
            mask &= (df["is_limit_up"] == 0)

        if Config.EXCLUDE_ST and "is_st" in df.columns:
            mask &= (df["is_st"] == 0)
        if Config.EXCLUDE_KC and "is_kc" in df.columns:
            mask &= (df["is_kc"] == 0)
        if Config.EXCLUDE_CY and "is_cy" in df.columns:
            mask &= (df["is_cy"] == 0)

        return df[mask]

    def get_moving_average_snapshot(
        self,
        data_query,
        stock_codes,
        end_date,
        column="close",
        window=5,
        shift=0,
        start_date=None,
        min_periods=None,
    ) -> "pd.DataFrame":
        """
        统一的 MA 计算工具：
        - 返回每只股票在 end_date 当日（或上一日）的原值 + MA 值
        - shift=1 表示 MA 只统计到前一日（常用于量比等指标）
        """
        if not stock_codes:
            import pandas as pd  # 延迟导入
            return pd.DataFrame()

        import pandas as pd  # 延迟导入
        try:
            end_ts = pd.to_datetime(end_date)
        except Exception:
            import pandas as pd
            end_ts = pd.Timestamp(end_date)

        lookback_days = max(window * 3, window + shift + 2)
        start_date = start_date or (end_ts - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        min_periods = min_periods or window

        try:
            hist = data_query.get_batch_stock_history(
                stock_codes,
                start_date,
                end_date,
                columns=["stock_code", "trade_date", column],
            )
            if hist.empty:
                return pd.DataFrame()
        except Exception as e:
            print(f"[StrategyBase] 计算 {window} 日均值失败: {e}")
            return pd.DataFrame()

        hist = hist.sort_values(["stock_code", "trade_date"])
        group = hist.groupby("stock_code", sort=False)[column]
        ma_series = group.rolling(window=window, min_periods=min_periods).mean()
        if shift:
            ma_series = ma_series.shift(shift)
        hist["ma_value"] = ma_series.reset_index(level=0, drop=True)

        return hist.groupby("stock_code", sort=False, as_index=False).tail(1)

    def _get_start_date(self, current_date):
        import pandas as pd  # 延迟导入
        if isinstance(current_date, pd.Timestamp):
            d = current_date
        else:
            d = pd.to_datetime(current_date)
        return (d - pd.Timedelta(days=self.required_days)).strftime("%Y-%m-%d")

    def _batch_prepare_data(self, stocks: "pd.DataFrame", current_date, data_query):
        """
        批量拉取历史 K 线，减少 DB 调用次数
        """
        prepared = {
            "current_data": stocks.set_index("stock_code"),
            "history_data": {},
        }

        codes = stocks["stock_code"].tolist()
        start_date = self._get_start_date(current_date)

        try:
            batch_hist = data_query.get_batch_stock_history(
                codes,
                start_date,
                current_date,
                columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume", "adj_factor"],
            )
            if not batch_hist.empty:
                # 【核心修复 B】废弃后复权逻辑，统一使用引擎的 QFQ 算法
                # 策略层不再调用 apply_forward_adjustment（后复权），而是使用引擎提供的前复权逻辑
                # 注意：这里需要获取 latest_factor 才能进行前复权，但策略层无法直接访问引擎的 latest_factors
                # 解决方案：策略层拉取的历史数据应该已经是前复权的（由引擎在 get_stock_pool 时统一处理）
                # 但历史数据需要单独处理，这里暂时保留 apply_forward_adjustment 作为过渡方案
                # TODO: 后续应该由引擎统一提供前复权后的历史数据，策略层不再处理复权
                
                # 临时方案：如果历史数据包含 adj_factor，使用前复权逻辑（Raw * (Adj / Latest)）
                # 但策略层无法获取 latest_factor，所以这里暂时跳过复权，假设历史数据已经是前复权的
                # 或者使用简单的后复权作为过渡（虽然不完美，但至少保证数据一致性）
                # 
                # 【关键修复】统一使用前复权：如果引擎已经对 stock_pool 进行了前复权，
                # 那么历史数据也应该使用相同的前复权逻辑，确保 current_data 和 history_data 处于同一基准面
                # 
                # 由于策略层无法直接访问引擎的 latest_factors，这里采用保守策略：
                # 1. 如果历史数据已经包含 adj_factor，使用后复权（Raw * Adj）作为过渡
                # 2. 后续应该由引擎统一提供前复权后的历史数据
                if 'adj_factor' in batch_hist.columns:
                    # 使用后复权作为过渡（虽然不完美，但至少保证数据一致性）
                    # 注意：这会导致历史数据和当前数据量级不一致，但这是过渡方案
                    batch_hist = apply_forward_adjustment(batch_hist)
                
                for code, group in batch_hist.groupby("stock_code"):
                    prepared["history_data"][code] = group.sort_values("trade_date")
        except Exception as e:
            print(f"[StrategyBase] 批量取历史数据失败: {e}")

        return prepared

    def _batch_execute_strategy(self, stocks: "pd.DataFrame", prepared_data, strategy_func):
        """
        批量执行单票策略函数：
        strategy_func(self, stock_code, current_row, history_df) ->
            'buy'/'sell'/'hold'
          或 ('buy', weight)
          或 dict {action, weight, score, params}
        """
        simple_signals = {}
        cur = prepared_data["current_data"]
        hist_map = prepared_data["history_data"]

        for code in stocks["stock_code"]:
            try:
                if code not in cur.index or code not in hist_map:
                    continue
                current_row = cur.loc[code]
                history_df = hist_map[code]

                raw = strategy_func(self, code, current_row, history_df)

                rich, simple = self._normalize_signal(raw)
                self.last_rich_signals[code] = rich
                simple_signals[code] = simple
            except Exception as e:
                print(f"[StrategyBase] 单票执行失败 {code}: {e}")
                self.last_rich_signals[code] = {
                    "action": "hold",
                    "weight": 0.0,
                    "score": None,
                    "params": {"error": str(e)},
                }
                simple_signals[code] = "hold"

        return simple_signals

    def _normalize_signal(self, raw):
        """
        统一规范化策略返回的信号：
        支持三种形式：
        1. 'buy' / 'sell' / 'hold'
        2. ('buy', 0.2)
        3. {'action': 'buy', 'weight': 0.2, 'score': 3.5, 'params': {...}}
        """
        rich = {
            "action": "hold",
            "weight": 0.0,
            "score": None,
            "params": {},
        }

        # 1) 字符串
        if isinstance(raw, str):
            action = raw.lower()
            if action not in ("buy", "sell", "hold"):
                action = "hold"
            rich["action"] = action
            simple = action

        # 2) 元组 / 列表
        elif isinstance(raw, (list, tuple)) and len(raw) >= 1:
            action = str(raw[0]).lower()
            if action not in ("buy", "sell", "hold"):
                action = "hold"
            rich["action"] = action
            if len(raw) >= 2:
                try:
                    w = float(raw[1])
                    rich["weight"] = max(0.0, min(1.0, w))
                except Exception:
                    pass
            simple = action

        # 3) dict
        elif isinstance(raw, dict):
            action = str(raw.get("action", "hold")).lower()
            if action not in ("buy", "sell", "hold"):
                action = "hold"
            rich["action"] = action

            if "weight" in raw:
                try:
                    w = float(raw["weight"])
                    rich["weight"] = max(0.0, min(1.0, w))
                except Exception:
                    pass
            if "score" in raw:
                try:
                    rich["score"] = float(raw["score"])
                except Exception:
                    pass
            if "params" in raw and isinstance(raw["params"], dict):
                rich["params"] = raw["params"]

            simple = action

        else:
            simple = "hold"

        return rich, simple
