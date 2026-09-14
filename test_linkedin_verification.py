"""
test_linkedin_verification.py — Synthetic tests for leadership verification pipeline.

Tests use synthetic companies/people (NOT Postman/Supabase/Vapi) to validate
the generalized leadership inclusion/exclusion logic.
"""

import pytest
from preprocessor import (
    normalize_org_name,
    classify_page_type,
    extract_candidate_evidence_from_page,
    evaluate_candidate_affiliation,
)
from search_fallback import (
    normalize_org_name as sf_normalize_org,
    orgs_match,
    _extract_employer_from_snippet,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────────

def _make_team_page_html(people: list[dict], heading: str = "Our Team") -> str:
    """Generate a synthetic team page HTML with people cards."""
    cards = []
    for p in people:
        lk = f'<a href="{p["linkedin"]}">LinkedIn</a>' if p.get("linkedin") else ""
        cards.append(
            f'<div class="team-member">'
            f'<h3>{p["name"]}</h3>'
            f'<p>{p["role"]}</p>'
            f'{lk}'
            f'</div>'
        )
    return f"""
    <html><body>
    <section class="team">
        <h2>{heading}</h2>
        {''.join(cards)}
    </section>
    </body></html>
    """


def _make_testimonial_page_html(people: list[dict], heading: str = "Customer Stories") -> str:
    """Generate a synthetic testimonial page HTML."""
    quotes = []
    for p in people:
        org_text = f", {p['company']}" if p.get("company") else ""
        quotes.append(
            f'<blockquote class="testimonial">'
            f'<p>"{p.get("quote", "Great product!")}"</p>'
            f'<cite>{p["name"]} — {p["role"]}{org_text}</cite>'
            f'</blockquote>'
        )
    return f"""
    <html><body>
    <section class="customer-stories">
        <h2>{heading}</h2>
        {''.join(quotes)}
    </section>
    </body></html>
    """


def _make_mixed_page_html(
    team_people: list[dict],
    testimonial_people: list[dict],
) -> str:
    """Generate a page with both team members and customer testimonials."""
    team_cards = []
    for p in team_people:
        lk = f'<a href="{p["linkedin"]}">LinkedIn</a>' if p.get("linkedin") else ""
        team_cards.append(
            f'<div class="team-member">'
            f'<h3>{p["name"]}</h3>'
            f'<p>{p["role"]}</p>'
            f'{lk}'
            f'</div>'
        )

    quotes = []
    for p in testimonial_people:
        org_text = f", {p['company']}" if p.get("company") else ""
        quotes.append(
            f'<blockquote class="testimonial">'
            f'<p>"{p.get("quote", "Great product!")}"</p>'
            f'<cite>{p["name"]} — {p["role"]}{org_text}</cite>'
            f'</blockquote>'
        )

    return f"""
    <html><body>
    <section class="team">
        <h2>Our Leadership</h2>
        {''.join(team_cards)}
    </section>
    <section class="customer-stories">
        <h2>Customer Testimonials</h2>
        {''.join(quotes)}
    </section>
    </body></html>
    """


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Employee + matching LinkedIn → INCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_employee_with_matching_linkedin_included():
    """Employee on team page with matching LinkedIn should be INCLUDED."""
    html = _make_team_page_html([
        {"name": "Alice Chen", "role": "CEO", "linkedin": "https://linkedin.com/in/alicechen"},
    ])
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/about",
        html=html,
        target_domain="acmecorp.com",
    )
    assert len(candidates) >= 1
    alice = [c for c in candidates if "Alice" in c.name]
    assert len(alice) == 1, f"Expected Alice Chen, got: {[c.name for c in candidates]}"

    affiliation = evaluate_candidate_affiliation(alice[0], "acmecorp.com")
    assert affiliation.affiliation == "TARGET_COMPANY"
    assert affiliation.confidence >= 0.7


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Employee + strong team-page evidence + no LinkedIn → INCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_employee_on_team_page_no_linkedin_included():
    """Employee on official team page without LinkedIn should be INCLUDED."""
    html = _make_team_page_html([
        {"name": "Bob Martinez", "role": "CTO"},
    ])
    candidates = extract_candidate_evidence_from_page(
        page_url="https://widgetworks.io/team",
        html=html,
        target_domain="widgetworks.io",
    )
    bobs = [c for c in candidates if "Bob" in c.name]
    assert len(bobs) == 1, f"Expected Bob Martinez, got: {[c.name for c in candidates]}"

    affiliation = evaluate_candidate_affiliation(bobs[0], "widgetworks.io")
    assert affiliation.affiliation == "TARGET_COMPANY"
    assert affiliation.confidence >= 0.7


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Customer executive + LinkedIn shows customer company → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_customer_executive_excluded():
    """Customer executive in testimonial should be EXCLUDED."""
    html = _make_testimonial_page_html([
        {"name": "Carol Zhang", "role": "CEO", "company": "OtherCorp",
         "quote": "This product changed our workflow completely."},
    ])
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/customers",
        html=html,
        target_domain="acmecorp.com",
    )
    carols = [c for c in candidates if "Carol" in c.name]
    if carols:
        affiliation = evaluate_candidate_affiliation(carols[0], "acmecorp.com")
        assert affiliation.affiliation != "TARGET_COMPANY", \
            f"Customer executive should not be TARGET_COMPANY: {affiliation}"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Partner executive → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_partner_executive_excluded():
    """Partner executive mentioned in partner section should be EXCLUDED."""
    html = """
    <html><body>
    <section>
        <h2>Our Partners</h2>
        <div class="card">
            <p>David Kumar, CEO of PartnerTech, has been a great partner in our ecosystem.</p>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/partners",
        html=html,
        target_domain="acmecorp.com",
    )
    davids = [c for c in candidates if "David" in c.name]
    if davids:
        affiliation = evaluate_candidate_affiliation(davids[0], "acmecorp.com")
        assert affiliation.affiliation != "TARGET_COMPANY"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Same-name LinkedIn profile with different employer → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_different_employer_linkedin_rejected():
    """LinkedIn snippet showing different employer should be EXCLUDED."""
    # Test the employer extraction logic directly
    snippet = "Eva Rodriguez - CTO at CompetitorInc · Experience: CompetitorInc - Present"
    employer = _extract_employer_from_snippet(snippet, "acmecorp.com")
    # Should NOT be "TARGET" since CompetitorInc != acmecorp
    assert employer != "TARGET", f"Should not match target company, got: {employer}"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Former employee + different current employer → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_former_employee_different_employer_rejected():
    """Former employee whose LinkedIn shows different current employer should be EXCLUDED."""
    snippet = "Frank Lee - VP Engineering at NewStartup · Previously at AcmeCorp"
    employer = _extract_employer_from_snippet(snippet, "acmecorp.com")
    # NewStartup is current, AcmeCorp is historical → should not return TARGET
    assert employer != "TARGET", f"Former employee should not match: {employer}"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: Ambiguous LinkedIn identity → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_ambiguous_linkedin_excluded():
    """When LinkedIn employer can't be determined, should not verify."""
    snippet = "Grace Park - Professional"  # No employer info
    employer = _extract_employer_from_snippet(snippet, "acmecorp.com")
    # Should be None (ambiguous)
    assert employer is None, f"Ambiguous identity should return None, got: {employer}"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: Customer testimonial → EXCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_customer_testimonial_excluded():
    """Person in a testimonial/quote block should be EXCLUDED."""
    html = """
    <html><body>
    <section>
        <h2>What Our Customers Say</h2>
        <blockquote class="testimonial">
            <p>"Absolutely love this product!"</p>
            <cite>Henry Wilson — CTO, CustomerCo</cite>
        </blockquote>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/testimonials",
        html=html,
        target_domain="acmecorp.com",
    )
    henrys = [c for c in candidates if "Henry" in c.name]
    if henrys:
        affiliation = evaluate_candidate_affiliation(henrys[0], "acmecorp.com")
        assert affiliation.affiliation != "TARGET_COMPANY"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Genuine founder mentioned in company press release → INCLUDE
# ═══════════════════════════════════════════════════════════════════════════════

def test_founder_in_press_release_included():
    """Founder explicitly introduced in company context should be INCLUDED."""
    html = """
    <html><body>
    <section class="team">
        <h2>Leadership</h2>
        <div class="bio">
            <h3>Isabel Torres</h3>
            <p>Co-Founder and CEO. Isabel founded AcmeCorp in 2020 to revolutionize data infrastructure.</p>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/about",
        html=html,
        target_domain="acmecorp.com",
    )
    isabels = [c for c in candidates if "Isabel" in c.name]
    assert len(isabels) >= 1, f"Expected Isabel Torres, got: {[c.name for c in candidates]}"

    affiliation = evaluate_candidate_affiliation(isabels[0], "acmecorp.com")
    assert affiliation.affiliation == "TARGET_COMPANY"
    assert affiliation.confidence >= 0.7


# ═══════════════════════════════════════════════════════════════════════════════
# Test 10: Employee and customer both on same page → correctly distinguish
# ═══════════════════════════════════════════════════════════════════════════════

def test_employee_and_customer_on_same_page_distinguished():
    """When both employee and customer are on the same page, correctly classify each."""
    html = _make_mixed_page_html(
        team_people=[
            {"name": "Jack Rivera", "role": "CTO"},
        ],
        testimonial_people=[
            {"name": "Karen Smith", "role": "VP Engineering", "company": "ClientCo",
             "quote": "Best tool we've ever used."},
        ],
    )
    candidates = extract_candidate_evidence_from_page(
        page_url="https://acmecorp.com/about",
        html=html,
        target_domain="acmecorp.com",
    )

    jacks = [c for c in candidates if "Jack" in c.name]
    karens = [c for c in candidates if "Karen" in c.name]

    # Jack should be TARGET_COMPANY (team section)
    if jacks:
        jack_affil = evaluate_candidate_affiliation(jacks[0], "acmecorp.com")
        assert jack_affil.affiliation == "TARGET_COMPANY", \
            f"Jack (team member) should be TARGET_COMPANY: {jack_affil}"

    # Karen should NOT be TARGET_COMPANY (testimonial section)
    if karens:
        karen_affil = evaluate_candidate_affiliation(karens[0], "acmecorp.com")
        assert karen_affil.affiliation != "TARGET_COMPANY", \
            f"Karen (customer) should not be TARGET_COMPANY: {karen_affil}"


# ═══════════════════════════════════════════════════════════════════════════════
# Organization Normalization Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrgNormalization:
    def test_normalize_with_inc(self):
        assert sf_normalize_org("Supabase Inc.") == sf_normalize_org("Supabase")

    def test_normalize_with_llc(self):
        assert sf_normalize_org("Postman LLC") == sf_normalize_org("Postman")

    def test_normalize_domain(self):
        assert sf_normalize_org("acmecorp.com") == sf_normalize_org("AcmeCorp")

    def test_orgs_match_exact(self):
        assert orgs_match("Supabase", "Supabase Inc.") is True

    def test_orgs_match_domain_vs_name(self):
        assert orgs_match("postman.com", "Postman") is True

    def test_orgs_dont_match(self):
        assert orgs_match("Supabase", "Vercel") is False

    def test_normalize_with_comma_inc(self):
        assert sf_normalize_org("Acme, Inc.") == sf_normalize_org("Acme")


class TestEmployerExtraction:
    def test_target_employer(self):
        result = _extract_employer_from_snippet(
            "John Doe - CEO at AcmeCorp · San Francisco",
            "acmecorp.com"
        )
        assert result == "TARGET"

    def test_different_employer(self):
        result = _extract_employer_from_snippet(
            "Jane Doe - CTO at CompetitorCo · New York",
            "acmecorp.com"
        )
        assert result != "TARGET"
        assert result is not None  # Should identify CompetitorCo

    def test_no_employer_info(self):
        result = _extract_employer_from_snippet(
            "Some Person - Professional",
            "acmecorp.com"
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for Strict Leadership Verification & Anti-Contamination (Rules 1 - 6)
# ═══════════════════════════════════════════════════════════════════════════════

def test_person_company_contamination_separation():
    """Rule 1 & 2: Name must not contain company name, associated_company must be captured."""
    html = """
    <html><body>
    <section>
        <h2>Individual investors</h2>
        <div class="card">
            <h1>Cassidy Williams</h1>
            <div>Netlify DevRel</div>
        </div>
        <div class="card">
            <h1>Lee Robinson</h1>
            <div>Vercel DevRel</div>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://supabase.com/company",
        html=html,
        target_domain="supabase.com",
    )
    assert len(candidates) == 2
    cassidy = [c for c in candidates if "Cassidy" in c.name][0]
    lee = [c for c in candidates if "Lee" in c.name][0]

    # Verify name does NOT concatenate company
    assert cassidy.name == "Cassidy Williams"
    assert "Netlify" not in cassidy.name
    assert cassidy.associated_company == "Netlify"
    assert cassidy.role == "DevRel"

    assert lee.name == "Lee Robinson"
    assert "Vercel" not in lee.name
    assert lee.associated_company == "Vercel"
    assert lee.role == "DevRel"

    # Both must be rejected from target company
    cass_affil = evaluate_candidate_affiliation(cassidy, "supabase.com")
    lee_affil = evaluate_candidate_affiliation(lee, "supabase.com")
    assert cass_affil.affiliation != "TARGET_COMPANY"
    assert lee_affil.affiliation != "TARGET_COMPANY"


def test_investor_section_candidates_excluded():
    """Rule 1 & 3: Candidates listed under investor sections must be strictly excluded."""
    html = """
    <html><body>
    <section>
        <h3>Our investors</h3>
        <h2>Individual investors</h2>
        <div class="investor-card">
            <p>Alex Solomon PagerDuty Cofounder</p>
        </div>
        <div class="investor-card">
            <p>Tom Preston-Werner GitHub Cofounder</p>
        </div>
        <div class="investor-card">
            <p>Pedro Canahuati 1Password CTO, Ex-Facebook</p>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://supabase.com/company",
        html=html,
        target_domain="supabase.com",
    )
    for c in candidates:
        affil = evaluate_candidate_affiliation(c, "supabase.com")
        assert affil.affiliation != "TARGET_COMPANY", f"{c.name} in investors should be excluded"
        assert "GitHub" not in c.name
        assert "PagerDuty" not in c.name
        assert "1Password" not in c.name


def test_customer_sales_page_testimonial_excluded():
    """Rule 3: Customer testimonial quote on sales page must be excluded."""
    html = """
    <html><body>
    <section class="quote">
        <p>Jakob Steinn, Co-founder & Tech Lead, Good Tape</p>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://supabase.com/contact/sales",
        html=html,
        target_domain="supabase.com",
    )
    assert len(candidates) >= 1
    jakob = candidates[0]
    assert jakob.name == "Jakob Steinn"
    assert not jakob.name.endswith("Co")
    assert jakob.associated_company == "Good Tape"

    affil = evaluate_candidate_affiliation(jakob, "supabase.com")
    assert affil.affiliation != "TARGET_COMPANY"


def test_obvious_first_party_leadership_included_without_linkedin():
    """Rule 3 & 5: Unmistakable official first-party leadership included even with linkedin_url = null."""
    html = """
    <html><body>
    <section class="founders">
        <h2>The founders</h2>
        <p>Postman started simply as a side project to solve a specific problem:
        Abhinav Asthana, Postman's CEO and co-founder, set out to create a tool.</p>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://postman.com/company/about-postman/",
        html=html,
        target_domain="postman.com",
    )
    assert len(candidates) >= 1
    abhinav = [c for c in candidates if "Abhinav" in c.name][0]
    assert abhinav.name == "Abhinav Asthana"
    assert abhinav.role in ["CEO", "CEO and co-founder", "CEO of Postman"]

    affil = evaluate_candidate_affiliation(abhinav, "postman.com")
    assert affil.affiliation == "TARGET_COMPANY"
    assert affil.confidence >= 0.85


def test_toc_buzzwords_and_unrelated_sentences_excluded():
    """Verify table-of-contents buzzwords and sentence-separated words are not extracted as candidates."""
    html = """
    <html><body>
    <div class="sidebar">
        <p>On this page Extreme effectiveness when working asynchronously Egoless Kaizen mindset Default to action Unreasonable levels of ambition A founder mindset</p>
    </div>
    <div class="article">
        <div class="author-byline">
            <h3>Ant Wilson</h3>
            <p>CTO and Co-Founder</p>
        </div>
        <p>An intense shipping schedule can lead to an accrual of technical debt. To solve for this during Kaizen Weeks...</p>
    </div>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://supabase.com/blog/who-we-hire",
        html=html,
        target_domain="supabase.com",
    )
    names = [c.name for c in candidates]
    assert "Ant Wilson" in names
    assert "Kaizen Weeks" not in names
    assert "Egoless Kaizen" not in names

