"""Cost estimation and the spend cap that aborts a live run.

Token counts come from the daemon's own responses and from the subject's
transcript; this module turns them into an estimate in US dollars against a
price table it owns outright. The table is never fetched: a harness that
asked the network what it costs would fail differently depending on the
network, and the number's only job is to stop a runaway run and to be
printed at the end.

The estimate is list-price arithmetic. A subject on a Claude subscription
is not billed per token at all, so treat the figure as "what this run would
have cost on the API", which is the right order of magnitude for a cap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when the accumulated estimate passes the configured cap."""


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens for one model family."""

    input_usd: float
    output_usd: float

    @property
    def cache_write_usd(self) -> float:
        """Cache creation is priced at 1.25x the input rate."""
        return self.input_usd * 1.25

    @property
    def cache_read_usd(self) -> float:
        """A cache read is priced at 0.1x the input rate -- the whole point of bumping."""
        return self.input_usd * 0.1


#: Published list prices per million tokens, keyed by the family substring
#: that appears in an API model id (``claude-haiku-4-5-20251001``).
PRICES: dict[str, Price] = {
    "haiku": Price(input_usd=1.0, output_usd=5.0),
    "sonnet": Price(input_usd=3.0, output_usd=15.0),
    "opus": Price(input_usd=5.0, output_usd=25.0),
}

#: Used when a model id matches no known family. Deliberately the most
#: expensive entry: an unknown model must never make a run look cheaper
#: than it is and slip past the cap.
FALLBACK_PRICE = PRICES["opus"]


def price_for(model: str | None) -> Price:
    """Return the price table entry for ``model``, falling back to the dearest.

    Matching is by family substring rather than exact id: model ids carry a
    version and a date that change far more often than the price tiers do.
    """
    name = (model or "").lower()
    for family, price in PRICES.items():
        if family in name:
            return price
    return FALLBACK_PRICE


def usage_cost_usd(model: str | None, usage: Mapping[str, object]) -> float:
    """Estimate the cost of one API call from its reported ``usage`` block.

    Accepts the shape both the daemon (``BumpResult.usage``) and the
    transcript emit; unknown or missing counters are read as zero rather
    than raising, since a usage block gaining a field must not break a run.
    """
    price = price_for(model)
    per_token = (
        _count(usage, "input_tokens") * price.input_usd
        + _count(usage, "output_tokens") * price.output_usd
        + _count(usage, "cache_creation_input_tokens") * price.cache_write_usd
        + _count(usage, "cache_read_input_tokens") * price.cache_read_usd
    )
    return per_token / 1_000_000


def _count(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


@dataclass
class Budget:
    """Running cost estimate with a hard cap.

    The cap is checked at the points a run can still stop cheaply -- before
    sending another turn, before another fork -- rather than after the fact,
    so exceeding it costs at most one more call than the caller's last check.
    """

    cap_usd: float
    total_usd: float = 0.0
    entries: list[tuple[str, float]] = field(default_factory=list)

    def add(self, label: str, model: str | None, usage: Mapping[str, object] | None) -> float:
        """Record one call's usage and return what it added to the estimate."""
        if not usage:
            return 0.0
        cost = usage_cost_usd(model, usage)
        self.total_usd += cost
        self.entries.append((label, cost))
        return cost

    @property
    def exceeded(self) -> bool:
        """True once the estimate has passed the cap."""
        return self.total_usd > self.cap_usd

    def check(self, next_step: str) -> None:
        """Raise :class:`BudgetExceeded` if ``next_step`` must not be paid for."""
        if self.exceeded:
            raise BudgetExceeded(
                f"estimated spend ${self.total_usd:.4f} exceeds the ${self.cap_usd:.2f} cap; "
                f"refusing to run {next_step}"
            )


__all__ = ["PRICES", "Budget", "BudgetExceeded", "Price", "price_for", "usage_cost_usd"]
