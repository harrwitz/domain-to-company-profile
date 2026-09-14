import pytest
from preprocessor import (
    strip_unwanted_tags,
    extract_people_cards_from_html,
    evaluate_candidate_affiliation,
    extract_candidate_evidence_from_page,
)

def test_strip_unwanted_tags():
    html = """
    <html>
        <head><title>Test Page</title></head>
        <body>
            <script>alert('bad');</script>
            <style>body { color: red; }</style>
            <svg><path d="M0 0h24v24H0z"/></svg>
            <nav><a href="/home">Home Navigation</a></nav>
            <footer><p>Copyright Boilerplate Footer</p></footer>
            <main>
                <h1>Main Heading</h1>
                <p>Essential body content here.</p>
            </main>
        </body>
    </html>
    """
    cleaned = strip_unwanted_tags(html)
    assert "<script>" not in cleaned
    assert "<style>" not in cleaned
    assert "<svg>" not in cleaned
    assert "<nav>" not in cleaned
    assert "<footer>" not in cleaned
    assert "Main Heading" in cleaned
    assert "Essential body content here." in cleaned

def test_extract_people_cards_from_html():
    html = """
    <div class="team-card">
        <h3>Jane Doe</h3>
        <p>Chief Executive Officer</p>
        <a href="https://www.linkedin.com/in/janedoe">LinkedIn Profile</a>
    </div>
    """
    snippets = extract_people_cards_from_html(html)
    assert len(snippets) > 0
    assert "Jane Doe" in snippets[0]
    assert "Chief Executive Officer" in snippets[0]
    assert "https://www.linkedin.com/in/janedoe" in snippets[0]

def test_case_a_genuine_employee():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <section>
        <h2>Our Leadership</h2>
        <div>
            Jane Doe
            CEO & Co-Founder
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/about", html, "acmecorp.com")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
    assert aff.affiliation == "TARGET_COMPANY"
    assert aff.confidence >= 0.7


def test_case_b_customer_executive():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <section>
        <h2>Customer Story</h2>
        <div>
            John Smith
            CEO at Zenith
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/customers", html, "acmecorp.com")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"


def test_case_c_testimonial():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <blockquote>
        "AcmeCorp transformed our API design workflow."
        John Smith, CTO at Zenith
    </blockquote>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/", html, "acmecorp.com")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"


def test_case_d_partner():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <section>
        <h2>Partner Spotlight</h2>
        <div>Jane Doe, Founder of PartnerCo</div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/partners", html, "acmecorp.com")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"


def test_case_e_genuine_founder_press():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <p>Jane Doe, Founder and CEO of AcmeCorp, announced the launch of their platform.</p>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/press", html, "acmecorp.com")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
    assert aff.affiliation == "TARGET_COMPANY"


def test_case_f_ambiguous():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <div>Jane Doe CEO</div>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/blog", html, "acmecorp.com")
    if candidates:
        aff = evaluate_candidate_affiliation(candidates[0], "acmecorp.com")
        assert aff.affiliation != "TARGET_COMPANY" or aff.confidence < 0.7


def test_case_g_customer_page_employee_and_customer():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <article>
        <h2>How Zenith Built Their Stack</h2>
        <p>Author: Jane Doe (AcmeCorp Team)</p>
        <blockquote>"Incredible tool!" - John Smith, CTO at Zenith</blockquote>
    </article>
    """
    candidates = extract_candidate_evidence_from_page("https://acmecorp.com/customers/zenith", html, "acmecorp.com")
    affiliations = [evaluate_candidate_affiliation(c, "acmecorp.com") for c in candidates]
    
    target_names = [a.name for a in affiliations if a.affiliation == "TARGET_COMPANY" and a.confidence >= 0.7]
    assert "John Smith" not in target_names


def test_case_h_arbitrary_organization_names():
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation
    html = """
    <section>
        <h2>Executive Board</h2>
        <div>Alice Miller, Chief Technology Officer at AlphaBetaCorp</div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://alphabetacorp.org/company", html, "alphabetacorp.org")
    assert len(candidates) > 0
    aff = evaluate_candidate_affiliation(candidates[0], "alphabetacorp.org")
    assert aff.affiliation == "TARGET_COMPANY"

