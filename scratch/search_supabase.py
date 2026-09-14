import asyncio
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def search_supabase_team():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        for url in [
            "https://supabase.com/company",
            "https://supabase.com/blog",
            "https://supabase.com/blog/supabase-series-b",
            "https://supabase.com/blog/supabase-series-a",
            "https://supabase.com/blog/page/1",
        ]:
            html, status = await fetch_page_html(page, url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                for name in ["Paul Copplestone", "Ant Wilson", "Copplestone", "Wilson", "Founder", "CEO"]:
                    if name in text:
                        print(f"Found '{name}' on {url}:")
                        idx = text.find(name)
                        print("  Snippet:", text[max(0, idx-50):min(len(text), idx+150)])

        await browser.close()

if __name__ == "__main__":
    asyncio.run(search_supabase_team())
