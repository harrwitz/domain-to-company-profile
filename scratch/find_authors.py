import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from crawler import crawl_domain
from bs4 import BeautifulSoup
import re

async def main():
    data = await crawl_domain('supabase.com')
    pages = data.get('pages', {})
    for url in ['https://supabase.com/company', 'https://supabase.com/blog/categories/company', 'https://supabase.com/blog/supabase-series-f']:
        html = pages.get(url, '')
        soup = BeautifulSoup(html, 'html.parser')
        print(f"=== {url} ===")
        for s in soup.find_all('script'):
            if not s.string:
                continue
            for m in re.finditer(r'author[^,}]+', s.string, re.I):
                print("  ", m.group(0)[:120])
            for m in re.finditer(r'(?:paul|ant|wilson|copplestone)[^,}]+', s.string, re.I):
                print("  NAME MATCH:", m.group(0)[:120])

if __name__ == "__main__":
    asyncio.run(main())
