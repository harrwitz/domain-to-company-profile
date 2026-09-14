import asyncio
import sys
from urllib.parse import unquote
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def search_google(name: str, company: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        query = f'site:linkedin.com/in "{name}" "{company}"'
        url = f"https://www.google.com/search?q={query}"
        print(f"Searching Google: {name} ({company})")
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "linkedin.com/in/" in href:
                clean = href.split("?")[0].split("&")[0].rstrip("/")
                if "google.com" not in clean:
                    print(f"  ✓ Direct Google match for {name}: {clean}")
                    await browser.close()
                    return clean
            if "/url?q=" in href and "linkedin.com/in/" in href:
                target = href.split("/url?q=")[1].split("&")[0]
                clean = unquote(target).split("?")[0].rstrip("/")
                print(f"  ✓ Decoded Google match for {name}: {clean}")
                await browser.close()
                return clean

        await browser.close()
        return None

if __name__ == "__main__":
    asyncio.run(search_google("Abhinav Asthana", "Postman"))
    asyncio.run(search_google("Paul Copplestone", "Supabase"))
    asyncio.run(search_google("Jason Mitura", "Vapi"))
