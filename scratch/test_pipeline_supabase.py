import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
from crawler import crawl_domain
from bs4 import BeautifulSoup
import re
import json

class EvidenceCategory:
    FIRST_PARTY_LEADERSHIP = "FIRST_PARTY_LEADERSHIP"
    FIRST_PARTY_COMPANY_ANNOUNCEMENT = "FIRST_PARTY_COMPANY_ANNOUNCEMENT"
    FIRST_PARTY_AUTHOR_BIO = "FIRST_PARTY_AUTHOR_BIO"
    FIRST_PARTY_EMPLOYEE = "FIRST_PARTY_EMPLOYEE"
    CUSTOMER_STORY = "CUSTOMER_STORY"
    TESTIMONIAL = "TESTIMONIAL"
    PARTNER_PAGE = "PARTNER_PAGE"
    EXTERNAL_COMPANY_REFERENCE = "EXTERNAL_COMPANY_REFERENCE"
    INVESTOR = "INVESTOR"
    AMBIGUOUS = "AMBIGUOUS"

async def test_run():
    data = await crawl_domain("supabase.com")
    pages = data.get("pages", {})
    print(f"Total crawled pages: {len(pages)}")
    
    # Check if Paul Copplestone or Ant Wilson appear in NEXT_DATA or text
    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            try:
                d = json.loads(nd.string)
                def search_d(o):
                    if isinstance(o, dict):
                        auth = o.get("author")
                        if auth and ("ant" in str(auth).lower() or "paul" in str(auth).lower()):
                            print(f"[{url}] Author in NEXT_DATA: {auth}, title={o.get('author_title')}")
                        for v in o.values(): search_d(v)
                    elif isinstance(o, list):
                        for item in o: search_d(item)
                search_d(d)
            except:
                pass

if __name__ == "__main__":
    asyncio.run(test_run())
