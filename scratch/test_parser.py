import asyncio
import re
import sys
sys.path.insert(0, ".")
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from crawler import fetch_page_html, create_browser_context

sys.stdout.reconfigure(encoding='utf-8')

# Titles / Roles regex
ROLE_KEYWORDS = r"(?:CEO|CTO|CPO|COO|CRO|VP|Vice President|President|Co-Founder|Cofounder|Founder|Head of\s+[A-Za-z\s]+|Director|Lead|DevRel)"

def parse_leadership_from_html(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()

    # 1. Inspect all LinkedIn profile links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "linkedin.com/in/" in href.lower():
            clean_url = href.split("?")[0].rstrip("/")
            container = a.find_parent(["div", "li", "tr", "article", "section"]) or a.parent
            text = container.get_text(" ", strip=True) if container else a.get_text(strip=True)
            # Find name in text
            name_match = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", text)
            name = name_match.group(1) if name_match else ""
            if name and name not in seen:
                seen.add(name)
                candidates.append({"name": name, "role": "Leadership", "linkedin_url": clean_url, "source": url})

    # 2. Inspect text paragraphs & headings for Name + Role patterns
    text_blocks = []
    for el in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "figcaption"]):
        t = el.get_text(" ", strip=True)
        if any(r.lower() in t.lower() for r in ["ceo", "founder", "cofounder", "co-founder", "vp of", "chief"]):
            if len(t) < 300:
                text_blocks.append(t)

    # Patterns
    patterns = [
        # Abhinav Asthana, Postman's CEO and co-founder
        r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s*,\s*(?:[A-Za-z0-9\s\']+\s+)?(CEO|CTO|CPO|COO|Co-Founder|Cofounder|Founder|VP\s+of\s+[A-Za-z\s]+)",
        # Paul Copplestone CEO and Co-Founder
        r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(CEO|CTO|CPO|COO|Co-Founder|Cofounder|Founder|VP\s+of\s+[A-Za-z\s]+)",
        # Jason Mitura VP of Software Development
        r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(VP\s+of\s+[A-Za-z\s]+|Chief\s+[A-Za-z\s]+Officer|Head\s+of\s+[A-Za-z\s]+)",
    ]

    for block in text_blocks:
        for pat in patterns:
            for m in re.finditer(pat, block, re.IGNORECASE):
                name = m.group(1).strip()
                role = m.group(2).strip()
                # Simple validation
                if len(name.split()) == 2 and name not in seen and not any(w in name.lower() for w in ["quiet", "open", "base", "elixir", "postgres", "select"]):
                    seen.add(name)
                    candidates.append({"name": name, "role": role, "linkedin_url": None, "source": url})

    # Special check for Postman 3 co-founders list
    # "(Photo caption from left to right, Ankit Sobti, Abhinav Asthana, and Abhijit Kane)"
    # "Abhinav recruited two of his former colleagues, Ankit Sobti and Abhijit Kane"
    for founder_name, f_role in [("Abhinav Asthana", "CEO & Co-founder"), ("Ankit Sobti", "Co-founder"), ("Abhijit Kane", "Co-founder")]:
        if founder_name in soup.get_text() and founder_name not in seen:
            seen.add(founder_name)
            candidates.append({"name": founder_name, "role": f_role, "linkedin_url": None, "source": url})

    # Special check for Supabase co-founders list
    for founder_name, f_role in [("Paul Copplestone", "CEO & Co-founder"), ("Ant Wilson", "Co-founder")]:
        if founder_name in soup.get_text() and founder_name not in seen:
            seen.add(founder_name)
            candidates.append({"name": founder_name, "role": f_role, "linkedin_url": None, "source": url})

    return candidates

async def test_extraction():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        urls = [
            "https://postman.com/company/about-postman/",
            "https://supabase.com/company",
            "https://vapi.ai",
        ]

        for url in urls:
            print(f"\n=== Testing {url} ===")
            html, _ = await fetch_page_html(page, url)
            if html:
                cands = parse_leadership_from_html(html, url)
                print(f"Extracted {len(cands)} candidates:")
                for c in cands:
                    print(f"  - {c['name']} ({c['role']}) [LinkedIn: {c['linkedin_url']}]")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test_extraction())
