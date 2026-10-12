import numpy as np
from typing import List, Tuple


def identify_extrema(prices: np.ndarray, min_pct: float = 0.03, min_bars: int = 2) -> List[Tuple[int, float, str]]:
    """
    Identify turning points (peaks and valleys) in a price series.

    Args:
        prices: Price sequence as numpy array.
        min_pct: Minimum percentage change threshold (0-1). Points with
            change less than this relative to previous extrema are filtered.
            Set to 0 to accept all extrema.
        min_bars: Window size for local extremum detection.

    Returns:
        List of tuples (index, price, type) sorted by index ascending.
        type is 'peak' or 'valley'.
    """
    if len(prices) < 3:
        return []

    n = len(prices)
    raw_extrema = []

    for i in range(n):
        window_start = max(0, i - min_bars)
        window_end = min(n, i + min_bars + 1)

        is_peak = True
        is_valley = True

        for j in range(window_start, window_end):
            if j != i:
                if prices[i] <= prices[j]:
                    is_peak = False
                if prices[i] >= prices[j]:
                    is_valley = False

        if is_peak or is_valley:
            raw_extrema.append((i, float(prices[i]), 'peak' if is_peak else 'valley'))

    filtered_extrema = []
    for idx, price, etype in raw_extrema:
        if min_pct == 0:
            filtered_extrema.append((idx, price, etype))
        elif len(filtered_extrema) == 0:
            filtered_extrema.append((idx, price, etype))
        else:
            prev_idx, prev_price, _ = filtered_extrema[-1]

            if etype == 'peak':
                pct_change = (price - prev_price) / prev_price
            else:
                pct_change = (prev_price - price) / price

            if pct_change >= min_pct:
                filtered_extrema.append((idx, price, etype))

    return filtered_extrema


def build_segments(prices: np.ndarray, extrema: List[Tuple[int, float, str]]) -> List[dict]:
    """
    Build trend segments from extrema points.

    Args:
        prices: Price sequence as numpy array.
        extrema: List of extrema from identify_extrema.

    Returns:
        List of segment dictionaries, each containing:
        - index_start, index_end: Start and end indices
        - price_start, price_end: Start and end prices
        - direction: 'up', 'down', or 'flat'
        - duration: Number of bars = index_end - index_start
        - pct_change: Price change percentage
        - price_range: Tuple of (high, low) prices in the segment
    """
    if len(extrema) < 2:
        return []

    n = len(prices)
    segments = []

    for i in range(len(extrema) - 1):
        ext1 = extrema[i]
        ext2 = extrema[i + 1]

        index_start, price_start = int(ext1[0]), float(ext1[1])
        index_end, price_end = int(ext2[0]), float(ext2[1])

        direction = 'up' if price_end > price_start else ('down' if price_end < price_start else 'flat')

        duration = index_end - index_start

        pct_change = (price_end - price_start) / price_start

        segment_prices = prices[index_start:index_end + 1]
        price_high = float(np.max(segment_prices))
        price_low = float(np.min(segment_prices))
        price_range = (price_high, price_low)

        segments.append({
            'index_start': index_start,
            'index_end': index_end,
            'price_start': price_start,
            'price_end': price_end,
            'direction': direction,
            'duration': duration,
            'pct_change': pct_change,
            'price_range': price_range,
        })

    return segments
