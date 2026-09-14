"""
config.py — Configuration & Settings

Loads environment variables and defines constants used across the pipeline.
"""

import os
import sys
from dotenv import load_dotenv
from rich.console import Console

console = Console()

# Load environment variables from .env file
load_dotenv()

# ─── API Keys ───────────────────────────────────────────────────────────────────

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

# ─── LLM Settings ───────────────────────────────────────────────────────────────

# Preferred model (user can override via .env)
LLM_MODEL: str = os.getenv("GEMINI_MODEL", os.getenv("LLM_MODEL", "gemini-3.8-flash"))

# Fallback order for free-tier Gemini models (do NOT include obsolete models)
GEMINI_MODEL_FALLBACK: list[str] = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]

LLM_TEMPERATURE: float = 0.1          # Low temperature for factual extraction
LLM_MAX_RETRIES: int = 5              # Max rate-limit retries
LLM_CONCURRENCY: int = int(os.getenv("LLM_CONCURRENCY", "1"))
LLM_REQUEST_DELAY_SECONDS: float = float(os.getenv("LLM_REQUEST_DELAY_SECONDS", "2.0"))

# ─── Crawler Settings ───────────────────────────────────────────────────────────

BROWSER_TIMEOUT_MS: int = 30_000      # 30 second timeout per page load
PAGE_WAIT_MS: int = 2_000             # Wait 2s after load for dynamic content
MAX_SUBPAGES: int = 8                 # Max subpages to crawl per domain
CRAWL_DELAY_S: float = 1.0            # Polite delay between page fetches

# URL patterns for relevant subpages to discover
RELEVANT_PATHS: list[str] = [
    "/about", "/about-us", "/about_us",
    "/team", "/our-team", "/people", "/leadership",
    "/company", "/company/about",
    "/contact", "/contact-us",
    "/pricing",
    "/careers",
]

# ─── Preprocessor Settings ───────────────────────────────────────────────────────

MAX_TOKENS_PER_PAGE: int = 1_500      # Approx token budget per page (~6K chars)
MAX_TOKENS_TOTAL: int = 4_000        # Total token budget for all pages per domain (~16K chars)
CHARS_PER_TOKEN: int = 4              # Rough approximation

# Tags to strip from HTML before processing
STRIP_TAGS: list[str] = [
    "script", "style", "svg", "nav", "footer", "header",
    "iframe", "noscript", "meta", "link", "button", "form",
    "input", "select", "textarea",
]

# ─── Search Fallback Settings ────────────────────────────────────────────────────

MAX_LINKEDIN_LOOKUPS: int = 5         # Max search queries per domain
SEARCH_DELAY_S: float = 1.0           # Rate limit between search queries

# ─── Cost Tracking (Gemini pricing configuration) ─────────────────────────────

# API Billing tier: "free" (default on Google AI Studio keys) or "paid"
GEMINI_TIER: str = os.getenv("GEMINI_TIER", "free").lower()

# Standard Pay-As-You-Go pricing per 1M tokens (USD)
# Reference: https://ai.google.dev/pricing
MODEL_PAYGO_PRICING: dict[str, dict[str, float]] = {
    "gemini-3.8-flash": {"input": 0.10, "output": 0.40},
    "gemini-3.7-flash": {"input": 0.10, "output": 0.40},
    "gemini-3.6-flash": {"input": 0.10, "output": 0.40},
    "gemini-3.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-3.5-flash-lite": {"input": 0.0375, "output": 0.15},
    "gemini-3.1-flash-lite": {"input": 0.0375, "output": 0.15},
    "gemini-2.5-flash-lite": {"input": 0.0375, "output": 0.15},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
}

COST_PER_1M_INPUT_TOKENS: float = 0.0 if GEMINI_TIER == "free" else 0.10
COST_PER_1M_OUTPUT_TOKENS: float = 0.0 if GEMINI_TIER == "free" else 0.40

# ─── Default Target Domains ─────────────────────────────────────────────────────

DEFAULT_DOMAINS: list[str] = [
    "postman.com",
    "supabase.com",
    "vapi.ai",
]


def validate_config() -> bool:
    """Validate that required configuration is present."""
    if not GEMINI_API_KEY:
        console.print(
            "[bold red]ERROR:[/] GEMINI_API_KEY not set. "
            "Copy .env.example to .env and add your key.",
            style="red",
        )
        return False

    if not TAVILY_API_KEY or TAVILY_API_KEY.startswith("tvly-your"):
        console.print(
            "[yellow]WARNING:[/] TAVILY_API_KEY not set. "
            "LinkedIn search fallback will be disabled.",
        )

    return True
