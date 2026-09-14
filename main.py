"""
main.py — CLI Entry Point & Pipeline Orchestrator

Orchestrates the full lead enrichment pipeline:
  1. Crawl each domain with Playwright
  2. Preprocess HTML → clean markdown
  3. Extract intelligence via LLM (Instructor + OpenAI)
  4. Enrich with LinkedIn search fallback (optional)
  5. Track costs and generate output files

Usage:
  python main.py                                    # Run with default 3 domains
  python main.py --domains postman.com supabase.com  # Custom domains
  python main.py --verbose                           # Enable debug logging
"""

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from config import DEFAULT_DOMAINS, validate_config, LLM_CONCURRENCY, LLM_REQUEST_DELAY_SECONDS
from models import CompanyIntelligence, DomainResult, PipelineOutput
from crawler import crawl_domain
from preprocessor import preprocess_domain
from extractor import extract_intelligence, validate_and_select_model, get_selected_model
from search_fallback import enrich_linkedin_urls, get_verification_metrics
from cost_tracker import CostTracker
from utils import logger, normalize_domain, setup_logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console()


async def crawl_and_preprocess_domain(domain: str) -> dict:
    """Phase 1: Crawl and preprocess a single domain."""
    domain = normalize_domain(domain)
    errors: list[str] = []

    console.print(
        Panel(
            f"[bold white]Phase 1 - Crawling: {domain}[/bold white]",
            style="blue",
            width=60,
        )
    )

    # ── Step 1: Crawl ──────────────────────────────────────────────────────
    try:
        crawl_data = await crawl_domain(domain)
        pages = crawl_data.get("pages", {})
        crawl_stats = crawl_data.get("crawl_stats")
        direct_linkedin = crawl_data.get("linkedin_links", [])
        crawled_emails = crawl_data.get("emails", [])
    except Exception as e:
        error_msg = f"Crawling failed: {type(e).__name__}: {str(e)[:200]}"
        logger.error(f"  ❌ {error_msg}")
        errors.append(error_msg)
        pages = {}
        crawl_stats = None
        direct_linkedin = []
        crawled_emails = []

    if not pages:
        logger.error(f"  ❌ No pages could be crawled for {domain}")
        return {
            "domain": domain,
            "content": "",
            "all_emails": [],
            "direct_linkedin": [],
            "pages_crawled": 0,
            "crawl_stats": crawl_stats,
            "errors": errors + ["No pages could be crawled"],
            "failed_early": True,
        }

    # ── Step 2: Preprocess ─────────────────────────────────────────────────
    try:
        content, emails = preprocess_domain(pages, domain, linkedin_links=direct_linkedin, crawl_stats=crawl_stats)
        all_emails = list(dict.fromkeys(crawled_emails + emails))
    except Exception as e:
        error_msg = f"Preprocessing failed: {type(e).__name__}: {str(e)[:200]}"
        logger.error(f"  ❌ {error_msg}")
        errors.append(error_msg)
        content, all_emails = "", crawled_emails

    # ── Domain Discovery & Leadership Diagnostics ──────────────────────────
    if crawl_stats:
        red_excl = crawl_data.get("redirect_exclusions", 0) if "crawl_data" in locals() else 0
        plat_excl = crawl_data.get("platform_exclusions", 0) if "crawl_data" in locals() else 0
        diag_lines = [
            f"[bold]Candidate links discovered:[/] {crawl_stats.candidate_links_found}",
            f"[bold]Leadership-relevant pages discovered:[/] {crawl_stats.leadership_pages_found}",
            f"[bold]Pages crawled:[/] {crawl_stats.pages_crawled} (failed: {crawl_stats.pages_failed})",
            f"[bold]Redirect / duplicate exclusions:[/] {red_excl}",
            f"[bold]Corporate-vs-platform/UGC exclusions:[/] {plat_excl}",
            f"[bold]Leadership candidates detected:[/] {crawl_stats.candidates_detected}",
            f"[bold]Candidates verified (1st-party):[/] [green]{crawl_stats.candidates_verified}[/green]",
            f"[bold]Candidates rejected (external/unsupported):[/] [red]{crawl_stats.candidates_rejected}[/red]",
            "\n[bold]Selected Pages & Relevance Diagnostics:[/bold]",
        ]
        for sp in crawl_stats.selected_pages:
            title_part = f" | Title: \"{sp.title}\"" if sp.title else ""
            lead_tag = " [bold magenta][LEADERSHIP][/bold magenta]" if sp.is_leadership_relevant else ""
            diag_lines.append(f"  • [yellow]Score {sp.score:2d}[/yellow]{lead_tag} {sp.url}{title_part}")
            diag_lines.append(f"    [dim]Reason: {sp.reason}[/dim]")

        console.print(Panel("\n".join(diag_lines), title=f"🔍 Domain Diagnostics: {domain}", border_style="cyan"))

    if not content:
        logger.error(f"  ❌ No content could be extracted for {domain}")
        return {
            "domain": domain,
            "content": "",
            "all_emails": all_emails,
            "direct_linkedin": direct_linkedin,
            "pages_crawled": len(pages),
            "crawl_stats": crawl_stats,
            "errors": errors + ["Content extraction yielded empty results"],
            "failed_early": True,
        }

    return {
        "domain": domain,
        "content": content,
        "all_emails": all_emails,
        "direct_linkedin": direct_linkedin,
        "pages_crawled": len(pages),
        "crawl_stats": crawl_stats,
        "errors": errors,
        "failed_early": False,
    }

async def extract_and_enrich_domain(
    ctx: dict,
    cost_tracker: CostTracker,
    semaphore: asyncio.Semaphore,
    selected_model: str = "",
) -> DomainResult:
    """Phase 2: LLM Extraction and LinkedIn Enrichment."""
    domain = ctx["domain"]
    errors = ctx["errors"]

    if ctx.get("failed_early"):
        return DomainResult(
            domain=domain,
            intelligence=CompanyIntelligence(
                company_overview=f"Processing failed early for {domain}.",
                target_audience="Unknown — processing failed.",
                contact_points=ctx.get("all_emails", []),
                key_leadership=[],
                data_confidence_score=0.0,
            ),
            pages_crawled=ctx.get("pages_crawled", 0),
            crawl_stats=ctx.get("crawl_stats"),
            model_used=selected_model,
            errors=errors,
        )

    console.print(
        Panel(
            f"[bold white]Phase 2 - LLM Extraction: {domain}[/bold white]",
            style="magenta",
            width=60,
        )
    )

    async with semaphore:
        if LLM_REQUEST_DELAY_SECONDS > 0:
            await asyncio.sleep(LLM_REQUEST_DELAY_SECONDS)

        # ── Step 3: LLM Extraction ─────────────────────────────────────────────
        try:
            intelligence, usage_info = await extract_intelligence(
                domain, ctx["content"], ctx["all_emails"], direct_linkedin_links=ctx["direct_linkedin"]
            )
        except Exception as e:
            error_msg = f"LLM extraction failed: {type(e).__name__}: {str(e)[:200]}"
            logger.error(f"  ❌ {error_msg}")
            errors.append(error_msg)
            intelligence = CompanyIntelligence(
                company_overview=f"LLM extraction failed for {domain}.",
                target_audience="Unknown — LLM extraction failed.",
                contact_points=ctx["all_emails"],
                key_leadership=[],
                data_confidence_score=0.1,
            )
            usage_info = {}

    # ── Step 4: LinkedIn Enrichment (Bonus) ────────────────────────────────
    try:
        intelligence.key_leadership = await enrich_linkedin_urls(
            intelligence.key_leadership, domain
        )
    except Exception as e:
        error_msg = f"LinkedIn enrichment failed: {type(e).__name__}: {str(e)[:100]}"
        logger.warning(f"  ⚠ {error_msg}")
        errors.append(error_msg)

    # ── Step 5: Track Costs ────────────────────────────────────────────────
    domain_cost = cost_tracker.record_usage(domain, usage_info)

    return DomainResult(
        domain=domain,
        intelligence=intelligence,
        pages_crawled=ctx["pages_crawled"],
        crawl_stats=ctx["crawl_stats"],
        model_used=selected_model,
        cost_usd=domain_cost.cost_usd,
        tokens_used=domain_cost.total_tokens,
        errors=errors if errors else [],
    )


def write_output_json(output: PipelineOutput, filepath: Path) -> None:
    """Write the pipeline output to a JSON file."""
    data = output.model_dump(mode="json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"📄 Output written to: {filepath}")


def write_output_csv(output: PipelineOutput, filepath: Path) -> None:
    """Write a flattened summary to a CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Domain",
            "Company Overview",
            "Target Audience",
            "Contact Emails",
            "Key Leadership",
            "Confidence Score",
            "Pages Crawled",
            "Tokens Used",
            "Cost (USD)",
        ])

        for result in output.results:
            intel = result.intelligence
            leadership_str = "; ".join(
                f"{m.name} ({m.role})"
                + (f" [{m.linkedin_url}]" if m.linkedin_url else "")
                for m in intel.key_leadership
            )
            writer.writerow([
                result.domain,
                intel.company_overview,
                intel.target_audience,
                ", ".join(intel.contact_points),
                leadership_str,
                f"{intel.data_confidence_score:.2f}",
                result.pages_crawled,
                result.tokens_used or 0,
                f"${result.cost_usd:.6f}" if result.cost_usd else "$0.000000",
            ])

    logger.info(f"📄 CSV output written to: {filepath}")


def print_results_table(output: PipelineOutput) -> None:
    """Print a rich summary table of results to the terminal."""
    table = Table(
        title="🏢 Lead Enrichment Results",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
        width=120,
    )
    table.add_column("Domain", style="cyan", width=16)
    table.add_column("Overview", width=40)
    table.add_column("ICP", width=25)
    table.add_column("Emails", width=15)
    table.add_column("Leaders", justify="center", width=8)
    table.add_column("Score", justify="center", width=6)

    for result in output.results:
        intel = result.intelligence
        # Truncate overview for table display
        overview = intel.company_overview
        if len(overview) > 100:
            overview = overview[:97] + "..."

        icp = intel.target_audience
        if len(icp) > 60:
            icp = icp[:57] + "..."

        emails = ", ".join(intel.contact_points[:3])
        if len(intel.contact_points) > 3:
            emails += f" +{len(intel.contact_points)-3}"

        score = intel.data_confidence_score
        score_color = "green" if score >= 0.7 else "yellow" if score >= 0.4 else "red"

        table.add_row(
            result.domain,
            overview,
            icp,
            emails,
            str(len(intel.key_leadership)),
            f"[{score_color}]{score:.2f}[/{score_color}]",
        )

    console.print()
    console.print(table)


async def main(domains: list[str], verbose: bool = False) -> None:
    """Main pipeline orchestrator.

    Args:
        domains: List of target domains to process
        verbose: Enable debug logging
    """
    if verbose:
        setup_logging(verbose=True)

    # ── Banner ─────────────────────────────────────────────────────────────
    console.print()
    console.print(
        Panel(
            "[bold cyan]🚀 Autonomous Lead Enrichment Agent[/bold cyan]\n"
            f"[dim]Processing {len(domains)} domain(s)[/dim]",
            style="cyan",
            width=60,
        )
    )
    console.print()

    # ── Validate Config ────────────────────────────────────────────────────
    if not validate_config():
        sys.exit(1)

    # ── Validate & Select Gemini Model ─────────────────────────────────────
    try:
        selected_model = await validate_and_select_model()
    except RuntimeError as e:
        console.print(f"[bold red]ERROR:[/] {e}", style="red")
        sys.exit(1)

    # ── Process Domains: Phase 1 (Crawling) ──────────────────────────────────
    cost_tracker = CostTracker(model=selected_model)
    start_time = time.time()
    
    contexts = []
    for i, domain in enumerate(domains, 1):
        if i > 1:
            await asyncio.sleep(5.0)  # Rate limiting for crawler
        console.print(f"\n{'─' * 60}")
        console.print(
            f"[bold]Phase 1: Domain {i}/{len(domains)}[/bold]",
            style="white",
        )
        ctx = await crawl_and_preprocess_domain(domain)
        contexts.append(ctx)

    # ── Process Domains: Phase 2 (LLM Extraction) ────────────────────────────
    console.print(f"\n{'═' * 60}")
    console.print("[bold cyan]Phase 2: LLM Extraction & Enrichment[/bold cyan]")
    
    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
    results: list[DomainResult] = []
    
    # Process concurrently bounded by semaphore
    tasks = [extract_and_enrich_domain(ctx, cost_tracker, semaphore, selected_model) for ctx in contexts]
    
    for completed_task in asyncio.as_completed(tasks):
        try:
            result = await completed_task
            results.append(result)
        except Exception as e:
            logger.error(f"  ❌ Unexpected error during phase 2: {type(e).__name__}: {str(e)[:200]}")

    elapsed = time.time() - start_time

    # ── Assemble Output ───────────────────────────────────────────────────
    output = PipelineOutput(
        results=results,
        total_domains=len(results),
        model_used=selected_model,
        total_cost_usd=cost_tracker.total_cost_usd,
        total_tokens=cost_tracker.total_tokens,
    )

    # ── Write Output Files ────────────────────────────────────────────────
    output_dir = Path(__file__).parent
    output_subfolder = output_dir / "output"
    output_subfolder.mkdir(exist_ok=True)

    # Write to root and output/ subfolder
    write_output_json(output, output_dir / "output.json")
    write_output_csv(output, output_dir / "output.csv")
    write_output_json(output, output_subfolder / "output.json")
    write_output_csv(output, output_subfolder / "output.csv")

    # ── Print Summary ─────────────────────────────────────────────────────
    console.print(f"\n{'═' * 60}")
    print_results_table(output)
    cost_tracker.print_summary()

    v_metrics = get_verification_metrics()
    total_candidate_links = sum(r.crawl_stats.candidate_links_found for r in output.results if r.crawl_stats)
    total_leadership_links = sum(r.crawl_stats.leadership_pages_found for r in output.results if r.crawl_stats)
    total_pages_crawled = sum(r.pages_crawled for r in output.results)
    total_cand_detected = sum(r.crawl_stats.candidates_detected for r in output.results if r.crawl_stats)
    total_cand_verified = sum(r.crawl_stats.candidates_verified for r in output.results if r.crawl_stats)
    total_cand_rejected = sum(r.crawl_stats.candidates_rejected for r in output.results if r.crawl_stats)

    console.print(
        Panel(
            f"selected_model: {selected_model}\n"
            f"total_tokens: {cost_tracker.total_tokens}\n"
            f"total_cost: ${cost_tracker.total_cost_usd:.6f}\n"
            f"candidate_links_discovered: {total_candidate_links}\n"
            f"leadership_pages_discovered: {total_leadership_links}\n"
            f"pages_crawled: {total_pages_crawled}\n"
            f"leadership_candidates_detected: {total_cand_detected or v_metrics.candidates_found}\n"
            f"leadership_candidates_verified: {total_cand_verified or v_metrics.candidates_verified}\n"
            f"leadership_candidates_rejected: {total_cand_rejected or v_metrics.candidates_rejected}\n"
            f"LinkedIn searches performed: {v_metrics.searches_performed}\n"
            f"LinkedIn matches found: {v_metrics.matches_found}",
            title="🎯 Pipeline Diagnostics & Verification Report",
            style="bold green",
            width=65,
        )
    )

    console.print(f"\n⏱  Total time: {elapsed:.1f}s")
    console.print(f"📄 Output files: output.json, output.csv, output/output.json, output/output.csv")
    console.print()


def cli() -> None:
    """Parse command-line arguments and run the pipeline."""
    parser = argparse.ArgumentParser(
        description="Autonomous Lead Enrichment Agent — "
        "Crawl company websites and extract structured intelligence using LLMs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py\n"
            "  python main.py --domains postman.com supabase.com vapi.ai\n"
            "  python main.py --verbose\n"
        ),
    )
    parser.add_argument(
        "--domains",
        nargs="+",
        default=DEFAULT_DOMAINS,
        help=f"Target domains to process (default: {', '.join(DEFAULT_DOMAINS)})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()
    asyncio.run(main(args.domains, args.verbose))


if __name__ == "__main__":
    cli()
