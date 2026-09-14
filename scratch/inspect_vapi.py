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
        for url in ["https://vapi.ai/careers", "https://vapi.ai/blog"]:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            print(f"=== {url} ===")
            text = soup.get_text(" ", strip=True)
            for kw in ["founder", "ceo", "cto", "leadership", "team", "jordan", "nikita"]:
                count = text.lower().count(kw)
                print(f"  kw '{kw}': count={count}")
        await b.close()

if __name__ == "__main__":
    asyncio.run(test())
