import asyncio
import re
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

KNOWN_THIRD_PARTY_COMPANIES = {
    "netlify", "vercel", "docker", "pagerduty", "github", "retool", "fly",
    "logdna", "airtable", "instagram", "infinitus", "makerpad", "pipedream",
    "worklife", "rogue capital", "quiet capital", "chapter one", "hashicorp",
    "retune", "loopp", "stripe", "openai", "google", "meta", "microsoft"
}

def clean_and_verify_candidates(raw_candidates: list[dict], target_domain: str) -> list[dict]:
    domain_name = target_domain.split(".")[0].lower()
    verified_map: dict[str, dict] = {}

    for cand in raw_candidates:
        name = cand["name"].strip()
        role = cand["role"].strip()
        url = cand["linkedin_url"]
        source = cand["source"]

        # 1. Reject first-name-only or invalid names
        parts = name.split()
        if len(parts) < 2:
            continue
        
        # 2. Check third party company roles / titles
        combined_text = f"{name} {role}".lower()
        if any(tp in combined_text for tp in KNOWN_THIRD_PARTY_COMPANIES):
            print(f"  [REJECTED - Third Party] {name} ({role})")
            continue

        if any(r in role.lower() for r in ["investor", "customer", "partner", "podcast guest"]):
            print(f"  [REJECTED - Non-Employee Role] {name} ({role})")
            continue

        # 3. Canonical name normalization (e.g. Abhinav Asthana)
        canonical_key = f"{parts[0].lower()} {parts[-1].lower()}"

        if canonical_key in verified_map:
            # Merge LinkedIn URL if missing
            if not verified_map[canonical_key]["linkedin_url"] and url:
                verified_map[canonical_key]["linkedin_url"] = url
            # Keep longer, more specific role if default
            if verified_map[canonical_key]["role"] == "Key Team Member" and role != "Key Team Member":
                verified_map[canonical_key]["role"] = role
        else:
            verified_map[canonical_key] = {
                "name": name,
                "role": role,
                "linkedin_url": url,
                "source": source
            }

    return list(verified_map.values())

async def test():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        for domain, test_url in [
            ("postman.com", "https://postman.com/company/about-postman/"),
            ("supabase.com", "https://supabase.com/company"),
            ("vapi.ai", "https://vapi.ai")
        ]:
            print(f"\n==================== {domain} ====================")
            html, _ = await fetch_page_html(page, test_url)
            if html:
                from preprocessor import extract_leadership_candidates_from_pages
                from crawler import extract_linkedin_links_from_html
                linkedin_links = extract_linkedin_links_from_html(html, test_url)
                raw_cands = extract_leadership_candidates_from_pages({test_url: html}, linkedin_links)
                print(f"Raw candidates count: {len(raw_cands)}")
                for r in raw_cands:
                    print(f"  Raw: {r['name']} | Role: {r['role']} | LinkedIn: {r['linkedin_url']}")
                
                cleaned = clean_and_verify_candidates(raw_cands, domain)
                print(f"\nVerified & Deduplicated candidates for {domain}: {len(cleaned)}")
                for c in cleaned:
                    print(f"  ✓ {c['name']} ({c['role']}) [LinkedIn: {c['linkedin_url']}]")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
