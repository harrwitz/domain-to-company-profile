import asyncio
import os
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def analyze():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        for url in [
            'https://postman.com/company/about-postman/',
            'https://supabase.com/about',
            'https://vapi.ai/about',
            'https://vapi.ai/team',
            'https://vapi.ai/company'
        ]:
            print(f"\n==================== {url} ====================")
            html, status = await fetch_page_html(page, url)
            if not html:
                print(f"Failed to load {url} (status: {status})")
                continue
            
            soup = BeautifulSoup(html, "html.parser")
            print("Text length:", len(soup.get_text()))
            
            # Find all linkedin links
            links = soup.find_all("a", href=True)
            linkedin_links = []
            for a in links:
                href = a["href"].strip()
                if "linkedin.com" in href.lower():
                    parent_text = a.parent.get_text(strip=True) if a.parent else ""
                    container = a.find_parent(["div", "li", "section", "article"])
                    container_text = container.get_text(" ", strip=True) if container else ""
                    linkedin_links.append((href, a.get_text(strip=True), container_text[:150]))
            
            print(f"LinkedIn links found: {len(linkedin_links)}")
            for href, text, ctx in linkedin_links:
                print(f"  URL: {href}")
                print(f"  Anchor text: '{text}'")
                print(f"  Container snippet: '{ctx}'\n")

            # Check for names or titles in text
            print("Searching for team / leadership keywords in page text...")
            text_content = soup.get_text("\n", strip=True)
            lines = [line for line in text_content.split("\n") if line.strip()]
            for line in lines:
                l_lower = line.lower()
                if any(kw in l_lower for kw in ["ceo", "cto", "cpo", "co-founder", "founder", "executive", "vp ", "head of", "director", "leadership", "board"]):
                    print(f"  Match line: {line[:120]}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(analyze())
