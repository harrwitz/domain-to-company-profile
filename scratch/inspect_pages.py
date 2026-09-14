import asyncio
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

async def inspect_pages():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        # 1. Supabase Company Page
        print("\n==================== https://supabase.com/company ====================")
        html, status = await fetch_page_html(page, "https://supabase.com/company")
        if html:
            soup = BeautifulSoup(html, "html.parser")
            print("Supabase /company text snippet:")
            print(soup.get_text(" ", strip=True)[:1500])
            links = soup.find_all("a", href=True)
            linkedin_links = [a["href"] for a in links if "linkedin" in a["href"].lower()]
            print("LinkedIn links on /company:", linkedin_links)
            for a in links:
                if "linkedin.com/in/" in a["href"].lower():
                    print("  Direct profile:", a["href"], "Text:", a.get_text(strip=True))

        # 2. Postman About Page
        print("\n==================== https://postman.com/company/about-postman/ ====================")
        html, status = await fetch_page_html(page, "https://postman.com/company/about-postman/")
        if html:
            soup = BeautifulSoup(html, "html.parser")
            print("Postman /company/about-postman text snippet:")
            print(soup.get_text(" ", strip=True)[:1500])
            links = soup.find_all("a", href=True)
            for a in links:
                if "linkedin" in a["href"].lower():
                    print("  Postman LinkedIn link:", a["href"], "Text:", a.get_text(strip=True))

        # 3. Vapi pages (careers, blog, main page)
        print("\n==================== vapi.ai pages ====================")
        for path in ["/", "/careers", "/blog"]:
            v_url = f"https://vapi.ai{path}"
            html, status = await fetch_page_html(page, v_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                print(f"--- {v_url} ---")
                text = soup.get_text(" ", strip=True)
                print("Length:", len(text))
                links = soup.find_all("a", href=True)
                for a in links:
                    if "linkedin" in a["href"].lower():
                        print("  Vapi LinkedIn link:", a["href"], "Text:", a.get_text(strip=True))

        await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_pages())
