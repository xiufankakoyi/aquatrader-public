"""SVG 分时图生成器（极致压缩版）。

提供将 1 分钟价格序列渲染为轻量 SVG 的能力，供盘中监控推送使用。
输出为可直接内联到 HTML 的 <svg> 字符串，不经过 base64 编码，避免膨胀。

设计约束：
- PushPlus 微信渠道单条内容长度限制约 2 万字（约 60KB UTF-8）。
- 单张 SVG 控制在 200~500 字节，4 指数 + 4 股票约 3~4KB。

优化策略：
- 使用 RDP（Ramer-Douglas-Peucker）算法抽稀到 15~30 个关键点。
- 使用 <polyline> 而非 <path>，坐标取整。
- 省略 xmlns、width/height 等冗余属性。
- y 轴使用实际价格区间，确保波动可见。
- 左侧显示最高/昨收/最低刻度，右侧显示当前价标签。
"""

from __future__ import annotations

from typing import Any


def _fmt_price(price: float) -> str:
    """根据价格大小选择最短的合理精度。

    Args:
        price: 价格数值。

    Returns:
        格式化后的价格字符串。
    """
    if price >= 1000:
        return f"{price:.0f}"
    if price >= 100:
        return f"{price:.1f}"
    return f"{price:.2f}"


def _smooth(prices: list[float], window: int = 3) -> list[float]:
    """简单移动平均平滑，减少高频锯齿。

    Args:
        prices: 原始价格序列。
        window: 平滑窗口大小。

    Returns:
        平滑后的价格序列，长度与输入相同。
    """
    if len(prices) < window:
        return prices
    half = window // 2
    result = []
    for i in range(len(prices)):
        start = max(0, i - half)
        end = min(len(prices), i + half + 1)
        result.append(sum(prices[start:end]) / (end - start))
    return result


def _rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker 曲线抽稀。

    Args:
        points: (x, y) 坐标序列。
        epsilon: 点到直线距离阈值，小于该阈值的点被丢弃。

    Returns:
        抽稀后的关键点序列，首尾点始终保留。
    """
    if len(points) <= 2:
        return points

    x0, y0 = points[0]
    xn, yn = points[-1]
    dx = xn - x0
    dy = yn - y0
    max_dist = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        x, y = points[i]
        if dx == 0:
            dist = abs(x - x0)
        else:
            dist = abs(dy * x - dx * y + xn * y0 - yn * x0) / ((dy * dy + dx * dx) ** 0.5)
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist > epsilon:
        left = _rdp(points[: index + 1], epsilon)
        right = _rdp(points[index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def build_intraday_svg(
    data: list[dict[str, Any]],
    last_close: float,
    *,
    view_w: int = 120,
    view_h: int = 36,
) -> str:
    """构建极简 SVG 分时图，返回可直接内联 HTML 的字符串。

    Args:
        data: 分钟数据列表，每项至少包含 price。
        last_close: 昨收价，用于颜色判断和参考线。
        view_w: SVG 视图宽度。
        view_h: SVG 视图高度。

    Returns:
        极简 <svg>...</svg> 字符串；数据为空或 last_close 非法时返回空字符串。
    """
    if not data or last_close <= 0:
        return ""

    prices = [float(d.get("price", 0)) for d in data]
    if not prices or any(p <= 0 for p in prices):
        return ""

    min_p = min(prices)
    max_p = max(prices)
    price_range = max_p - min_p
    # 上下留 8px 边距，确保线条不贴边
    plot_h = view_h - 8
    plot_w = view_w - 28  # 左侧留 24px 给刻度，右侧留 4px

    # 轻微平滑，保留走势同时减少高频锯齿
    smoothed = _smooth(prices, window=3)

    def map_x(i: int) -> int:
        return 24 + int((i / max(1, len(data) - 1)) * plot_w)

    def map_y(p: float) -> int:
        # 价格越高 y 越小
        if price_range == 0:
            return 4 + plot_h // 2
        return 4 + int((1 - (p - min_p) / price_range) * plot_h)

    # 抽稀：epsilon 设为 y 方向 1.0 像素，约 240 点 -> 25~45 点
    raw_points = [(float(map_x(i)), float(map_y(p))) for i, p in enumerate(smoothed)]
    if len(raw_points) > 40:
        simplified = _rdp(raw_points, epsilon=1.0)
    else:
        simplified = raw_points

    points_str = " ".join(f"{int(x)},{int(y)}" for x, y in simplified)

    close = prices[-1]
    up = close >= last_close
    color = "#e74c3c" if up else "#27ae60"

    zero_y = map_y(last_close)
    # 昨收参考线：限制在绘图区域内
    ref_y = max(4, min(4 + plot_h, zero_y))

    # 纵坐标刻度：最高、最低；避免重叠时只保留最高
    max_y = map_y(max_p)
    min_y = map_y(min_p)
    label_svg = ""
    if abs(max_y - min_y) > 10:
        label_svg = (
            f'<text x="22" y="{max_y + 3}" font-size="7" fill="#7f8c8d" text-anchor="end">{_fmt_price(max_p)}</text>'
            f'<text x="22" y="{min_y + 3}" font-size="7" fill="#7f8c8d" text-anchor="end">{_fmt_price(min_p)}</text>'
        )
    else:
        label_svg = f'<text x="22" y="{max_y + 3}" font-size="7" fill="#7f8c8d" text-anchor="end">{_fmt_price(max_p)}</text>'

    return (
        f'<svg viewBox="0 0 {view_w} {view_h}">'
        f'{label_svg}'
        f'<line x1="24" y1="{ref_y}" x2="{view_w}" y2="{ref_y}" stroke="#bdc3c7" stroke-width="0.8" stroke-dasharray="2,2"/>'
        f'<polyline points="{points_str}" stroke="{color}" stroke-width="0.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


def _to_pct_series(prices: list[float], last_close: float) -> list[float]:
    """将价格序列转换为涨跌幅序列（%）。"""
    if last_close <= 0:
        return [0.0] * len(prices)
    return [(p - last_close) / last_close * 100 for p in prices]


def clean_minute_data(
    data: list[dict[str, Any]],
    last_close: float,
    *,
    max_deviation: float = 0.30,
    min_points: int = 10,
) -> list[dict[str, Any]]:
    """过滤偏离昨收过远的脏分钟数据。

    TDX 在非交易时段或部分品种上会返回异常价格（如 0.01、数百万），
    直接绘制会导致纵轴爆炸。本函数只保留落在 [last_close*(1-max_deviation),
    last_close*(1+max_deviation)] 区间内的点，避免脏数据污染图表。

    Args:
        data: 原始分钟数据，每项至少包含 price。
        last_close: 昨收价，作为过滤基准。
        max_deviation: 相对昨收的最大允许偏离比例。
        min_points: 过滤后至少保留的点数，否则视为无效返回空列表。

    Returns:
        清洗后的分钟数据；last_close 非法或数据不足时返回空列表。
    """
    if not data or last_close <= 0:
        return []

    lower = last_close * (1 - max_deviation)
    upper = last_close * (1 + max_deviation)
    cleaned = [
        d for d in data
        if lower <= float(d.get("price", 0)) <= upper
    ]
    return cleaned if len(cleaned) >= min_points else []


def build_dual_intraday_svg(
    data_a: list[dict[str, Any]],
    last_close_a: float,
    data_b: list[dict[str, Any]],
    last_close_b: float,
    *,
    name_a: str = "上证",
    name_b: str = "深证",
    view_w: int = 300,
    view_h: int = 140,
) -> str:
    """构建上证+深证同图分时图，纵轴为上证指数价格。

    深证指数以虚线叠加在上证指数价格坐标上：
    将深证价格按涨跌幅等比映射到上证昨收价位，
    从而共用同一条价格纵轴，只保留上证的点位/涨跌幅文本。

    Args:
        data_a: 上证分钟数据。
        last_close_a: 上证昨收。
        data_b: 深证分钟数据。
        last_close_b: 深证昨收。
        name_a: 主线名称（仅保留参数，不显示图例）。
        name_b: 叠加线名称（仅保留参数，不显示图例）。
        view_w: SVG 视图宽度。
        view_h: SVG 视图高度。

    Returns:
        可直接内联的 <svg> 字符串。
    """
    if not data_a or not data_b or last_close_a <= 0 or last_close_b <= 0:
        return ""

    prices_a = [float(d.get("price", 0)) for d in data_a]
    prices_b = [float(d.get("price", 0)) for d in data_b]
    if not prices_a or not prices_b:
        return ""

    smoothed_a = _smooth(prices_a)
    smoothed_b = _smooth(prices_b)

    # 将深证价格映射到上证价格坐标，保持涨跌幅一致
    mapped_b = [last_close_a * (p / last_close_b) for p in smoothed_b]

    all_prices = list(smoothed_a) + list(mapped_b)
    min_p = min(all_prices)
    max_p = max(all_prices)
    price_range = max_p - min_p

    # 上下留 8px 边距，左侧 24px 给刻度，右侧 4px
    plot_h = view_h - 8
    plot_w = view_w - 28

    def map_x(i: int, n: int) -> int:
        return 24 + int((i / max(1, n - 1)) * plot_w)

    def map_y(p: float) -> int:
        if price_range == 0:
            return 4 + plot_h // 2
        return 4 + int((1 - (p - min_p) / price_range) * plot_h)

    def build_points(price_series: list[float], n: int) -> str:
        raw = [(float(map_x(i, n)), float(map_y(p))) for i, p in enumerate(price_series)]
        simplified = _rdp(raw, epsilon=1.2) if len(raw) > 40 else raw
        return " ".join(f"{int(x)},{int(y)}" for x, y in simplified)

    points_a = build_points(smoothed_a, len(data_a))
    points_b = build_points(mapped_b, len(data_b))

    close_a = prices_a[-1]
    up_a = close_a >= last_close_a
    color_a = "#e74c3c" if up_a else "#27ae60"

    close_b = prices_b[-1]
    up_b = close_b >= last_close_b
    color_b = "#e74c3c" if up_b else "#27ae60"

    zero_y = map_y(last_close_a)
    ref_y = max(4, min(4 + plot_h, zero_y))

    # 纵坐标刻度：最高、最低
    max_y = map_y(max_p)
    min_y = map_y(min_p)
    if abs(max_y - min_y) > 10:
        label_svg = (
            f'<text x="22" y="{max_y + 3}" font-size="8" fill="#7f8c8d" text-anchor="end">{_fmt_price(max_p)}</text>'
            f'<text x="22" y="{min_y + 3}" font-size="8" fill="#7f8c8d" text-anchor="end">{_fmt_price(min_p)}</text>'
        )
    else:
        label_svg = f'<text x="22" y="{max_y + 3}" font-size="8" fill="#7f8c8d" text-anchor="end">{_fmt_price(max_p)}</text>'

    return (
        f'<svg viewBox="0 0 {view_w} {view_h}" style="width:100%;height:auto;max-height:{view_h}px;display:block;">'
        f'{label_svg}'
        f'<line x1="24" y1="{ref_y}" x2="{view_w}" y2="{ref_y}" stroke="#bdc3c7" stroke-width="0.8" stroke-dasharray="2,2"/>'
        f'<polyline points="{points_b}" stroke="{color_b}" stroke-width="0.8" fill="none" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="4,2"/>'
        f'<polyline points="{points_a}" stroke="{color_a}" stroke-width="1" fill="none" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )
