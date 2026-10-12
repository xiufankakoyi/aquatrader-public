from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return exp(-0.5 * value * value) / sqrt(2.0 * pi)


def black_scholes_greeks(
    underlying_price: float,
    strike: float,
    dte: int | float,
    option_type: str,
    implied_volatility: float,
    risk_free_rate: float = 0.045,
    dividend_yield: float = 0.0,
) -> Greeks | None:
    """Return delta, gamma, and per-day theta using a Black-Scholes approximation."""
    if (
        underlying_price <= 0
        or strike <= 0
        or dte <= 0
        or implied_volatility <= 0
        or option_type not in {"call", "put"}
    ):
        return None

    time_to_expiry = max(float(dte) / 365.0, 1e-8)
    sigma_sqrt_t = implied_volatility * sqrt(time_to_expiry)
    if sigma_sqrt_t <= 0:
        return None

    d1 = (
        log(underlying_price / strike)
        + (risk_free_rate - dividend_yield + 0.5 * implied_volatility**2) * time_to_expiry
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    discount_div = exp(-dividend_yield * time_to_expiry)
    discount_rate = exp(-risk_free_rate * time_to_expiry)
    pdf_d1 = _norm_pdf(d1)

    gamma = discount_div * pdf_d1 / (underlying_price * sigma_sqrt_t)
    first_theta_term = -(
        underlying_price * discount_div * pdf_d1 * implied_volatility
    ) / (2.0 * sqrt(time_to_expiry))

    if option_type == "call":
        delta = discount_div * _norm_cdf(d1)
        annual_theta = (
            first_theta_term
            - risk_free_rate * strike * discount_rate * _norm_cdf(d2)
            + dividend_yield * underlying_price * discount_div * _norm_cdf(d1)
        )
    else:
        delta = -discount_div * _norm_cdf(-d1)
        annual_theta = (
            first_theta_term
            + risk_free_rate * strike * discount_rate * _norm_cdf(-d2)
            - dividend_yield * underlying_price * discount_div * _norm_cdf(-d1)
        )

    return Greeks(delta=delta, gamma=gamma, theta=annual_theta / 365.0)
