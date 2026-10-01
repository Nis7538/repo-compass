"""Claude API prices, for the review agent's cost cap and its run log.

Every price below was copied on 2026-10-01 from the official pricing page,
https://platform.claude.com/docs/en/about-claude/pricing ("Model pricing" table,
first-party Claude API, global routing, standard speed, no batch discount). Prices
change; when they do, update the table and the date. A model that is not listed is
refused: a cost cap that cannot price a request is not a cap.

Cache writes are priced at the 5-minute rate (1.25x input), the TTL the agent uses.
"""

from dataclasses import dataclass

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input: float
    cache_write: float  # 5-minute cache write
    cache_read: float  # cache hit or refresh
    output: float


PRICES = {
    # Claude Opus 5.5: $4 input, $5 5m write, $0.20 hit (0.05x), $20 output. Source: SOURCE.
    "claude-opus-5-5": Price(4.00, 5.00, 0.20, 20.00),
    # Claude Opus 5: $5 input, $6.25 5m write, $0.50 hit, $25 output. Source: SOURCE.
    "claude-opus-5": Price(5.00, 6.25, 0.50, 25.00),
    # Claude Sonnet 5.5: $2 input, $2.50 5m write, $0.20 hit, $10 output. Source: SOURCE.
    "claude-sonnet-5-5": Price(2.00, 2.50, 0.20, 10.00),
    # Claude Sonnet 5: $2 input, $2.50 5m write, $0.20 hit, $10 output. Source: SOURCE.
    "claude-sonnet-5": Price(2.00, 2.50, 0.20, 10.00),
    # Claude Haiku 4.5: $1 input, $1.25 5m write, $0.10 hit, $5 output. Source: SOURCE.
    "claude-haiku-4-5": Price(1.00, 1.25, 0.10, 5.00),
    # Claude Fable 5.1: $10 input, $12.50 5m write, $0.25 hit (0.025x), $50 output. Source: SOURCE.
    "claude-fable-5-1": Price(10.00, 12.50, 0.25, 50.00),
}


class UnknownModelError(Exception):
    def __init__(self, model: str):
        known = ", ".join(sorted(PRICES))
        super().__init__(
            f"No price for model {model!r}, so the cost cap cannot be enforced. "
            f"Known: {known}. Add it to src/compass/agent/pricing.py from {SOURCE}."
        )


def price_of(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(model) from None


@dataclass
class Usage:
    """Token counts as the API reports them. input_tokens is the uncached part only."""

    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.output_tokens += other.output_tokens

    @property
    def prompt_tokens(self) -> int:
        """Everything the model read: uncached + written to cache + read from cache."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def cost(usage: Usage, price: Price) -> float:
    """US dollars for this usage."""
    return (
        usage.input_tokens * price.input
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
        + usage.output_tokens * price.output
    ) / 1_000_000
