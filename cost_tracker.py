"""
cost_tracker.py — Token Usage & Cost Logging (Bonus)

Tracks OpenAI API token consumption and estimates costs per domain
and across the entire pipeline run.
"""

from dataclasses import dataclass, field
from config import (
    COST_PER_1M_INPUT_TOKENS,
    COST_PER_1M_OUTPUT_TOKENS,
    LLM_MODEL,
    GEMINI_TIER,
    MODEL_PAYGO_PRICING,
)
from utils import logger


@dataclass
class DomainCost:
    """Cost tracking for a single domain."""
    domain: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model_used: str = LLM_MODEL


@dataclass
class CostTracker:
    """Aggregate cost tracker for the entire pipeline run."""
    model: str = LLM_MODEL
    tier: str = GEMINI_TIER
    domain_costs: list[DomainCost] = field(default_factory=list)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(dc.prompt_tokens for dc in self.domain_costs)

    @property
    def total_completion_tokens(self) -> int:
        return sum(dc.completion_tokens for dc in self.domain_costs)

    @property
    def total_tokens(self) -> int:
        return sum(dc.total_tokens for dc in self.domain_costs)

    @property
    def total_cost_usd(self) -> float:
        return sum(dc.cost_usd for dc in self.domain_costs)

    def get_pricing_rates(self) -> tuple[float, float]:
        """Get (input_price_per_1m, output_price_per_1m) for configured model and tier."""
        if self.tier == "free":
            return 0.0, 0.0
        clean_model = self.model.replace("models/", "")
        pricing = MODEL_PAYGO_PRICING.get(clean_model, {"input": 0.10, "output": 0.40})
        return pricing["input"], pricing["output"]

    def record_usage(
        self,
        domain: str,
        usage_info: dict,
    ) -> DomainCost:
        """Record token usage for a domain and calculate cost using model pricing.

        Args:
            domain: The domain that was processed
            usage_info: Dict with prompt_tokens, completion_tokens, total_tokens

        Returns:
            DomainCost object with computed costs
        """
        prompt_tokens = usage_info.get("prompt_tokens", 0)
        completion_tokens = usage_info.get("completion_tokens", 0)
        total_tokens = usage_info.get("total_tokens", 0)

        input_rate, output_rate = self.get_pricing_rates()
        input_cost = (prompt_tokens / 1_000_000) * input_rate
        output_cost = (completion_tokens / 1_000_000) * output_rate
        total_cost = input_cost + output_cost

        domain_cost = DomainCost(
            domain=domain,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=round(total_cost, 6),
            model_used=self.model,
        )
        self.domain_costs.append(domain_cost)

        return domain_cost

    def print_summary(self) -> None:
        """Print a formatted cost summary to the console."""
        from rich.table import Table
        from rich.console import Console

        console = Console()

        table = Table(
            title="💰 API Cost Summary",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Domain", style="white")
        table.add_column("Prompt Tokens", justify="right", style="dim")
        table.add_column("Completion Tokens", justify="right", style="dim")
        table.add_column("Total Tokens", justify="right", style="green")
        table.add_column("Cost (USD)", justify="right", style="yellow")

        for dc in self.domain_costs:
            table.add_row(
                dc.domain,
                f"{dc.prompt_tokens:,}",
                f"{dc.completion_tokens:,}",
                f"{dc.total_tokens:,}",
                f"${dc.cost_usd:.6f}",
            )

        # Total row
        table.add_section()
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{self.total_prompt_tokens:,}[/bold]",
            f"[bold]{self.total_completion_tokens:,}[/bold]",
            f"[bold]{self.total_tokens:,}[/bold]",
            f"[bold]${self.total_cost_usd:.6f}[/bold]",
        )

        input_rate, output_rate = self.get_pricing_rates()
        console.print()
        console.print(table)
        console.print(f"\n  Model: {self.model}")
        if self.tier == "free":
            console.print("  Tier: FREE TIER (configured rate: $0.0/1M tokens)")
        else:
            console.print(f"  Pricing: ${input_rate}/1M input, ${output_rate}/1M output")
