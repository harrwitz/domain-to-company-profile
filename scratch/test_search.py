import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import asyncio
from crawler import create_browser_context
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import urllib.parse

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await create_browser_context(b)
        page = await ctx.new_page()
        q = urllib.parse.quote('site:linkedin.com/in "Lee Robinson" "Vercel"')
        url = f"https://www.bing.com/search?q={q}"
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        results = soup.find_all("li", class_="b_algo")
        print(f"Bing found {len(results)} results")
        for r in results[:3]:
            h2 = r.find("h2")
            snip = r.find("p")
            a = h2.find("a") if h2 else None
            print("Title:", h2.get_text(strip=True) if h2 else None)
            print("URL:", a.get("href") if a else None)
            print("Snippet:", snip.get_text(strip=True) if snip else None)
            print("---")
        await b.close()

if __name__ == "__main__":
    asyncio.run(test())
