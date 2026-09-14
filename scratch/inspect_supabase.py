import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import asyncio
from crawler import create_browser_context
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await create_browser_context(b)
        page = await ctx.new_page()
        await page.goto("https://supabase.com/contact/sales", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        for s in ["Jakob", "Steinn"]:
            el = soup.find(string=lambda t: t and s.lower() in t.lower())
            if el:
                print(f"FOUND {s}: {el.parent.get_text(' ', strip=True)[:300]}")
        await b.close()

if __name__ == "__main__":
    asyncio.run(test())
