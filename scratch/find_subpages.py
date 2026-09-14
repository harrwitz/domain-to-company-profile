import asyncio
import os
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context, extract_candidate_links

sys.stdout.reconfigure(encoding='utf-8')

async def find_subpages():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        for domain in ['postman.com', 'supabase.com', 'vapi.ai']:
            print(f"\n==================== {domain} ====================")
            url = f"https://{domain}"
            html, status = await fetch_page_html(page, url)
            if not html:
                print(f"Failed to load {domain}")
                continue
            
            candidates = extract_candidate_links(html, url, domain, url)
            print(f"Found {len(candidates)} candidate links:")
            for cand_url, score in candidates[:25]:
                print(f"  {cand_url} [score={score}]")
                
            # Also check all links on page regardless of score
            soup = BeautifulSoup(html, "html.parser")
            all_links = soup.find_all("a", href=True)
            print(f"Total hrefs on page: {len(all_links)}")
            leadership_links = []
            for a in all_links:
                href = a["href"].strip()
                text = a.get_text(" ", strip=True)
                title = a.get("title") or a.get("aria-label") or ""
                combined = f"{href} {text} {title}".lower()
                if any(k in combined for k in ["team", "people", "leadership", "founders", "executives", "about", "management", "company", "our-team", "leadership-team", "investors", "press"]):
                    leadership_links.append((href, text, title))
            print(f"Leadership/Team/About related links on {domain}: {len(leadership_links)}")
            for href, text, title in leadership_links:
                print(f"  Href: '{href}' | Text: '{text}' | Title: '{title}'")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(find_subpages())
