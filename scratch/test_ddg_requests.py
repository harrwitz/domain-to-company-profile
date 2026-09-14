import requests
import sys
sys.stdout.reconfigure(encoding='utf-8')
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse, unquote

def search_ddg_requests(name: str, company: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }
    url = "https://html.duckduckgo.com/html/"
    data = {"q": f'site:linkedin.com/in "{name}" "{company}"'}
    resp = requests.post(url, data=data, headers=headers, timeout=10)
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "linkedin.com/in/" in href:
                clean = href.split("?")[0].rstrip("/")
                print(f"  ✓ Found DDG link for {name}: {clean}")
                return clean
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                if "uddg" in qs:
                    target = unquote(qs["uddg"][0])
                    if "linkedin.com/in/" in target:
                        clean = target.split("?")[0].rstrip("/")
                        print(f"  ✓ Decoded DDG link for {name}: {clean}")
                        return clean
    print(f"  ✗ DDG search returned no link for {name} (status {resp.status_code})")
    return None

if __name__ == "__main__":
    search_ddg_requests("Abhinav Asthana", "Postman")
    search_ddg_requests("Paul Copplestone", "Supabase")
    search_ddg_requests("Jason Mitura", "Vapi")
