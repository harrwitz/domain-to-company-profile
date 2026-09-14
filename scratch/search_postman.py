import asyncio
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def search_postman_names():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        html, status = await fetch_page_html(page, "https://postman.com/company/about-postman/")
        if html:
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text("\n", strip=True)
            for name in ["Abhinav", "Asthana", "Ankit", "Sobti", "Abhijit", "Kane", "CEO", "Founder", "Executive", "VP", "Leader"]:
                lines = [line for line in text.split("\n") if name.lower() in line.lower()]
                if lines:
                    print(f"Matches for '{name}':")
                    for l in lines[:5]:
                        print(f"  {l}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(search_postman_names())
