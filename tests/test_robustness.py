import pytest
from bs4 import BeautifulSoup, Tag
from preprocessor import (
    safe_get_attr,
    strip_unwanted_tags,
    preprocess_page,
    preprocess_domain,
    get_nearest_heading,
)
from utils import (
    is_valid_contact_email,
    extract_emails_from_text,
)


def test_safe_get_attr_handles_none_attrs():
    """Regression test: Calling get() on a tag where attrs is None must not raise AttributeError."""
    t = Tag(name="div")
    t.attrs = None
    # Must return default without raising AttributeError: 'NoneType' object has no attribute 'get'
    assert safe_get_attr(t, "style") == ""
    assert safe_get_attr(t, "class", "default-val") == "default-val"
    assert safe_get_attr(None, "class") == ""


def test_strip_unwanted_tags_with_none_attrs_in_tree():
    """Regression test: BeautifulSoup tree containing tags with attrs=None must not crash strip_unwanted_tags."""
    html = '<div><p style="color:red">Content</p><span>More</span></div>'
    soup = BeautifulSoup(html, "html.parser")
    # Simulate a tag whose attrs is None (as happens with malformed/special tags on GitHub)
    span = soup.find("span")
    span.attrs = None

    cleaned = strip_unwanted_tags(str(soup))
    assert "Content" in cleaned
    assert "More" in cleaned


def test_preprocess_domain_graceful_handling_of_malformed_and_none_pages():
    """Regression test: One malformed or None page must NOT crash preprocessing of the domain."""
    pages = {
        "https://example.com/": "<html><body><h1>Example Corp</h1><p>Legitimate content</p></body></html>",
        "https://example.com/broken": None,  # None page content
        "https://example.com/empty": "",     # Empty string content
        "https://example.com/about": "<html><body><h2>About Us</h2><p>About Example Corp</p></body></html>",
    }
    content, emails = preprocess_domain(pages, "example.com")
    assert len(content) > 0
    assert "Example Corp" in content
    assert "PAGE URL: https://example.com/" in content
    assert "PAGE URL: https://example.com/about" in content


def test_get_nearest_heading_with_non_tag_and_none_attrs():
    """Regression test: get_nearest_heading must safely handle None or tags with attrs=None."""
    t = Tag(name="div")
    t.attrs = None
    assert get_nearest_heading(t) == ""
    assert get_nearest_heading(None) == ""


def test_email_placeholder_rejection():
    """Regression test: Obvious placeholder emails must be rejected."""
    placeholders = [
        "you@domain.com",
        "user@domain.com",
        "yourname@domain.com",
        "test@domain.com",
        "name@domain.com",
        "someone@domain.com",
        "placeholder@domain.com",
        "first.last@domain.com",
    ]
    for email in placeholders:
        assert not is_valid_contact_email(email), f"Failed to reject placeholder: {email}"


def test_email_example_domain_rejection():
    """Regression test: Emails from example/reserved domains must be rejected."""
    example_emails = [
        "test@example.com",
        "user@example.com",
        "hello@example.com",
        "name@example.com",
        "info@example.com",
        "support@example.org",
        "admin@example.net",
        "user@test.invalid",
        "user@localhost",
    ]
    for email in example_emails:
        assert not is_valid_contact_email(email), f"Failed to reject example email: {email}"


def test_email_legitimate_company_emails_retained():
    """Regression test: Legitimate public and personal company addresses must be retained."""
    legitimate_emails = [
        "info@stripe.com",
        "support@stripe.com",
        "sales@postman.com",
        "security@github.com",
        "press@github.com",
        "legal@vercel.com",
        "talent@vapi.ai",
        "jane.diaz@stripe.com",
        "abhinav@postman.com",
        "ant@supabase.io",
    ]
    for email in legitimate_emails:
        assert is_valid_contact_email(email), f"Legitimate email was incorrectly rejected: {email}"


def test_email_malformed_rejected():
    """Regression test: Malformed email syntax must be rejected."""
    malformed = [
        "notanemail",
        "missingatsign.com",
        "bad@",
        "@nodomain.com",
        "bad@.com",
        "two@@domain.com",
        "image@logo.png",
        "script@app.js",
    ]
    for email in malformed:
        assert not is_valid_contact_email(email), f"Malformed email was not rejected: {email}"


def test_initiative_and_section_cards_rejected_as_leadership():
    """Regression test: Non-person initiative labels, reports, and CTAs must NOT create leadership candidates."""
    from preprocessor import extract_candidate_evidence_from_page

    html = """
    <html><body>
    <section>
        <h2>Global Initiatives</h2>
        <div class="card">
            <h3>Social Impact</h3>
            <p>Our commitment to social impact across open source communities.</p>
        </div>
        <div class="col-md-4 card">
            <h3>Octoverse Dive</h3>
            <p>A deep dive into data and trends across global development.</p>
        </div>
        <div class="card">
            <h3>Explore Social Impact</h3>
            <p>Read about our grants and non-profit initiatives.</p>
            <a href="/social-impact">Learn More</a>
        </div>
        <div class="card">
            <h3>Opportunity Champion</h3>
            <p>Empowering the next generation of engineers through opportunity programs.</p>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://example.com/about",
        html=html,
        target_domain="example.com",
    )
    # None of these section/initiative headings should produce candidates
    assert len(candidates) == 0


def test_genuine_leadership_card_retained():
    """Regression test: Explicit leadership cards with legitimate roles must be extracted."""
    from preprocessor import extract_candidate_evidence_from_page, evaluate_candidate_affiliation

    html = """
    <html><body>
    <section>
        <h2>Leadership Team</h2>
        <div class="team-member">
            <h3>Elena Rostova</h3>
            <p>Chief Technology Officer</p>
            <p>Elena leads the global engineering and infrastructure teams.</p>
        </div>
    </section>
    </body></html>
    """
    candidates = extract_candidate_evidence_from_page(
        page_url="https://example.com/about",
        html=html,
        target_domain="example.com",
    )
    assert len(candidates) == 1
    assert candidates[0].name == "Elena Rostova"
    assert candidates[0].role == "Chief Technology Officer"
    aff = evaluate_candidate_affiliation(candidates[0], "example.com")
    assert aff.affiliation == "TARGET_COMPANY"
    assert aff.verification_status == "verified"

