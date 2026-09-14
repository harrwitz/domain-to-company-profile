import asyncio
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def search_vapi_team():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        for url in [
            "https://vapi.ai",
            "https://vapi.ai/careers",
            "https://vapi.ai/blog",
            "https://vapi.ai/community",
        ]:
            html, status = await fetch_page_html(page, url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                text = soup.get_text(" ", strip=True)
                for keyword in ["Jordan Singer", "Singer", "Jordan", "CEO", "Founder", "Jason Mitura", "Mitura", "Team"]:
                    if keyword.lower() in text.lower():
                        print(f"Found '{keyword}' on {url}:")
                        idx = text.lower().find(keyword.lower())
                        print("  Snippet:", text[max(0, idx-50):min(len(text), idx+150)])

        await browser.close()

if __name__ == "__main__":
    asyncio.run(search_vapi_team())
