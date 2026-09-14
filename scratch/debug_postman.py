import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from bs4 import BeautifulSoup
from preprocessor import extract_candidate_evidence_from_page, parse_candidate_identity_and_employer

html = """
<section class="founders">
    <h2>The founders</h2>
    <p>Postman started simply as a side project to solve a specific problem:
    Abhinav Asthana, Postman's CEO and co-founder, set out to create a tool.</p>
</section>
"""
soup = BeautifulSoup(html, "html.parser")
section = soup.find("section")
p = soup.find("p")
print("Section parse:", parse_candidate_identity_and_employer(section, section.get_text(" ", strip=True), "postman.com"))
print("P parse:", parse_candidate_identity_and_employer(p, p.get_text(" ", strip=True), "postman.com"))
cands = extract_candidate_evidence_from_page("https://postman.com/company/about-postman/", html, "postman.com")
print("Candidates extracted:", [(c.name, c.role) for c in cands])
