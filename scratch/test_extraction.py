import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
from crawler import crawl_domain
from bs4 import BeautifulSoup
import json
import re

async def main():
    data = await crawl_domain("supabase.com")
    pages = data.get("pages", {})
    
    print("=== SEARCHING STRUCTURED NEXT_DATA ===")
    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            try:
                d = json.loads(nd.string)
                # Search recursively for author or team entries
                def search_obj(obj, path=""):
                    if isinstance(obj, dict):
                        if "author" in obj or "author_title" in obj:
                            print(f"Found author dict in {url} at {path}: {obj.get('author')}, title={obj.get('author_title')}, author_url={obj.get('author_url')}")
                        for k, v in obj.items():
                            search_obj(v, f"{path}.{k}")
                    elif isinstance(obj, list):
                        for i, item in enumerate(obj):
                            search_obj(item, f"{path}[{i}]")
                search_obj(d)
            except Exception as e:
                pass

    print("\n=== SEARCHING PODCAST / MEDIA ON /company ===")
    comp_html = pages.get("https://supabase.com/company", "")
    comp_soup = BeautifulSoup(comp_html, "html.parser")
    # Search links and media mentions
    for a in comp_soup.find_all(["a", "div", "p"]):
        txt = a.get_text(" ", strip=True)
        if ("paul copplestone" in txt.lower() or "ant wilson" in txt.lower()) and len(txt) < 150:
            print("PODCAST/MEDIA ITEM:", txt, "HREF:", a.get("href", ""))

if __name__ == "__main__":
    asyncio.run(main())
