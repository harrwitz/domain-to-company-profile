import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from crawler import crawl_domain
from preprocessor import (
    preprocess_domain,
    extract_candidate_evidence_from_page,
    evaluate_candidate_affiliation,
    parse_candidate_identity_and_employer
)
from bs4 import BeautifulSoup

async def main():
    data = await crawl_domain("supabase.com")
    pages = data.get("pages", {})
    print(f"Total crawled pages: {len(pages)}")

    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        if "paul" in text.lower() or "ant wilson" in text.lower() or "copplestone" in text.lower():
            print(f"\n--- Checking URL with matches: {url} ---")
            candidates = extract_candidate_evidence_from_page(url, html, "supabase.com")
            print(f"Candidates found on {url}: {len(candidates)}")
            for c in candidates:
                aff = evaluate_candidate_affiliation(c, "supabase.com")
                print(f"  Candidate: {c.name} | Role: {c.role} | Evidence: {c.evidence_type} | Affiliation: {aff.affiliation} (conf: {aff.confidence}) | Reason: {aff.reason}")

    print("\n--- RUNNING FULL PREPROCESS DOMAIN ---")
    markdown, emails = preprocess_domain(pages, "supabase.com")
    print("\n--- PREPROCESSED EVIDENCE HEADER ---")
    for line in markdown.splitlines()[:50]:
        print(line)

if __name__ == "__main__":
    asyncio.run(main())
