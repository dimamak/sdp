"""Spend guard for Lane B — plan.md §9/§11: a drained X balance must degrade
the radar (Lane B stops), never break it silently, and never gate posting
(which the radar never does anyway)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

PRICE_USD = {"post_read": 0.005, "user_read": 0.010}

WARN_AT = 0.8


def _month_start_iso(now: datetime) -> str:
    return now.strftime("%Y-%m-01T00:00:00+00:00")


def record(store, kind: str, units: int = 1, note: str | None = None) -> float:
    """Record a billable call and return its cost in USD."""
    unit_usd = PRICE_USD[kind]
    store.record_radar_spend(kind, units, unit_usd, note)
    return units * unit_usd


@dataclass
class BudgetStatus:
    spent: float
    budget: float
    pct: float
    blocked: bool


def status(cfg, store, now: datetime | None = None) -> BudgetStatus:
    now = now or datetime.now(timezone.utc)
    budget = float(cfg.get("radar.monthly_budget_usd", 5.0) or 0.0)
    spent = store.radar_spend_since(_month_start_iso(now))
    pct = (spent / budget) if budget > 0 else 1.0
    return BudgetStatus(spent=spent, budget=budget, pct=pct, blocked=budget > 0 and spent >= budget)


def maybe_warn(cfg, store, notify, now: datetime | None = None) -> None:
    """Fire the 80%-of-budget Telegram warning at most once per UTC month.

    `notify` is a plain callable(str) — the caller decides how it actually
    reaches the user (e.g. by scheduling a coroutine on the bot's event loop).
    """
    now = now or datetime.now(timezone.utc)
    st = status(cfg, store, now)
    if st.pct < WARN_AT or st.budget <= 0:
        return
    month_key = now.strftime("%Y-%m")
    if store.get_cursor("radar_budget_warned") == month_key:
        return
    store.set_cursor("radar_budget_warned", month_key)
    notify(f"⚠️ X reply radar has spent ${st.spent:.2f} of its ${st.budget:.2f}/mo budget "
           f"({st.pct:.0%}). Lane B stops at 100%; Lane A keeps working either way.")
