import pytest
from preprocessor import (
    extract_candidate_evidence_from_page,
    evaluate_candidate_affiliation,
    is_plausible_person_name,
    disambiguate_name_and_company,
    COMPOUND_ROLE_REGEX,
)
import re


def test_1_genuine_employee():
    """Test 1: Genuine first-party employee / leadership card on target company."""
    html = """
    <section>
        <h2>Executive Leadership</h2>
        <div class="team-card">
            <h3>Sarah Jenkins</h3>
            <p>Chief Product Officer</p>
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/team", html, "examplecorp.com")
    assert len(candidates) == 1
    assert candidates[0].name == "Sarah Jenkins"
    assert "Chief Product Officer" in candidates[0].role
    aff = evaluate_candidate_affiliation(candidates[0], "examplecorp.com")
    assert aff.affiliation == "TARGET_COMPANY"
    assert aff.confidence >= 0.70
    assert aff.verification_status == "verified"


def test_2_customer_executive():
    """Test 2: Customer executive must NOT be classified as target employee."""
    html = """
    <section>
        <h2>Customer Case Study</h2>
        <div class="case-study-card">
            <h3>Mark Vance</h3>
            <p>CTO at MegaEnterprise</p>
            <p>MegaEnterprise uses ExampleCorp for data storage.</p>
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/customers/mega", html, "examplecorp.com")
    assert len(candidates) >= 1
    cand = candidates[0]
    aff = evaluate_candidate_affiliation(cand, "examplecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"
    assert aff.verification_status == "rejected"


def test_3_partner_executive():
    """Test 3: Partner executive must NOT be classified as target employee."""
    html = """
    <section>
        <h2>Technology Partners</h2>
        <div>
            Daniel Craig, Founder and CEO of CloudMatrix
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/partners", html, "examplecorp.com")
    assert len(candidates) >= 1
    aff = evaluate_candidate_affiliation(candidates[0], "examplecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"
    assert aff.verification_status == "rejected"


def test_4_former_employee():
    """Test 4: Former employee / departed executive must be excluded."""
    html = """
    <section>
        <h2>Company History</h2>
        <div>
            Robert Hayes, Former CTO at ExampleCorp, stepped down in 2023.
        </div>
    </section>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/about", html, "examplecorp.com")
    assert len(candidates) >= 1
    aff = evaluate_candidate_affiliation(candidates[0], "examplecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"
    assert aff.verification_status == "rejected"


def test_5_same_name_person():
    """Test 5: Same-name person disambiguation across target vs external company."""
    # Person 1 is on the target team page
    html_target = """
    <section>
        <h2>Leadership</h2>
        <div>
            <h3>Alex Miller</h3>
            <p>CEO & Co-Founder</p>
        </div>
    </section>
    """
    # Person 2 with the same name is an external speaker from another company
    html_external = """
    <section>
        <h2>Guest Speaker</h2>
        <div>
            Alex Miller, VP of Engineering at OtherTech
        </div>
    </section>
    """
    cands_target = extract_candidate_evidence_from_page("https://alpha.com/team", html_target, "alpha.com")
    cands_external = extract_candidate_evidence_from_page("https://alpha.com/events", html_external, "alpha.com")

    assert len(cands_target) == 1
    assert len(cands_external) == 1

    aff_target = evaluate_candidate_affiliation(cands_target[0], "alpha.com")
    aff_external = evaluate_candidate_affiliation(cands_external[0], "alpha.com")

    assert aff_target.affiliation == "TARGET_COMPANY"
    assert aff_target.confidence >= 0.70

    assert aff_external.affiliation != "TARGET_COMPANY"
    assert aff_external.verification_status == "rejected"


def test_6_person_associated_with_another_company():
    """Test 6: Person explicitly associated with another company is excluded without hardcoding."""
    html = """
    <article>
        <h2>Community Spotlight</h2>
        <div>
            Cassidy Vance DeltaSoft — DevRel
        </div>
    </article>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/blog", html, "examplecorp.com")
    assert len(candidates) >= 1
    cand = candidates[0]
    # Name must not contain the external company name
    assert "DeltaSoft" not in cand.name
    aff = evaluate_candidate_affiliation(cand, "examplecorp.com")
    assert aff.affiliation != "TARGET_COMPANY"
    assert aff.verification_status == "rejected"


def test_7_genuine_first_party_founder():
    """Test 7: Genuine first-party founder on target domain accepted with high confidence."""
    html = """
    <main>
        <h2>Meet the Founders</h2>
        <div class="team-card">
            <h3>Elena Rostova</h3>
            <p>CEO & Co-Founder</p>
            <p>Elena co-founded ExampleCorp to modernize database infrastructure.</p>
        </div>
    </main>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/about", html, "examplecorp.com")
    assert len(candidates) == 1
    assert candidates[0].name == "Elena Rostova"
    aff = evaluate_candidate_affiliation(candidates[0], "examplecorp.com")
    assert aff.affiliation == "TARGET_COMPANY"
    assert aff.confidence >= 0.85
    assert aff.verification_status == "verified"


def test_8_non_person_phrase_containing_founder():
    """Test 8: Phrases like 'founder mindset' or 'founder mentality' must NEVER create candidates."""
    assert not is_plausible_person_name("founder mindset")
    assert not is_plausible_person_name("Founder Mindset")
    assert not is_plausible_person_name("Founder Mentality")

    html = """
    <article>
        <h2>Engineering Culture</h2>
        <p>We cultivate a founder mindset across every team.</p>
    </article>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/blog/culture", html, "examplecorp.com")
    # Must produce 0 candidates
    assert len(candidates) == 0


def test_9_non_person_phrase_containing_lead():
    """Test 9: Phrases like 'lead to an accrual...' must NEVER create candidates."""
    assert not is_plausible_person_name("lead to an accrual")
    assert not is_plausible_person_name("Lead To An Accrual")
    assert not is_plausible_person_name("lead to an accrual of technical debt")

    html = """
    <article>
        <h2>Refactoring Guidelines</h2>
        <p>Shortcut implementations often lead to an accrual of technical debt in core microservices.</p>
    </article>
    """
    candidates = extract_candidate_evidence_from_page("https://examplecorp.com/blog/refactoring", html, "examplecorp.com")
    # Must produce 0 candidates
    assert len(candidates) == 0


def test_10_clean_person_names():
    """Test 10: Strict validation of plausible 2-3 word human names vs invalid concepts."""
    # Valid human names
    assert is_plausible_person_name("Jane Doe")
    assert is_plausible_person_name("Paul Copplestone")
    assert is_plausible_person_name("Abhinav Asthana")
    assert is_plausible_person_name("Kevin van Gundy")
    assert is_plausible_person_name("Mary-Jane Watson")

    # Invalid non-person words, corporate concepts, and fragments
    assert not is_plausible_person_name("customer")
    assert not is_plausible_person_name("developer")
    assert not is_plausible_person_name("architectural")
    assert not is_plausible_person_name("Who We Hire")
    assert not is_plausible_person_name("Kaizen Weeks")
    assert not is_plausible_person_name("Platform Architecture")
    assert not is_plausible_person_name("Customer Experience")
    assert not is_plausible_person_name("ExampleCorp")  # Target company itself


def test_11_compound_roles():
    """Test 11: Compound roles are properly matched and extracted."""
    test_titles = [
        "CEO & Co-Founder",
        "Co-Founder & CTO",
        "Product Architect / Co-Founder",
        "Chief Technology Officer",
        "Founding Engineer",
        "Vice President",
        "Lead Architect",
    ]
    for title in test_titles:
        match = re.search(COMPOUND_ROLE_REGEX, title, re.IGNORECASE)
        assert match is not None, f"Failed to match compound role: {title}"


def test_12_non_person_initiatives_and_ctas():
    """Test 12: General non-person initiative labels, section headings, and CTAs must be rejected."""
    # Action verbs / CTAs
    assert not is_plausible_person_name("Explore Social Impact")
    assert not is_plausible_person_name("Read More")
    assert not is_plausible_person_name("Discover Features")
    assert not is_plausible_person_name("Download Guide")
    assert not is_plausible_person_name("Explore Diversity")

    # Initiatives / programs / sections
    assert not is_plausible_person_name("Octoverse Dive")
    assert not is_plausible_person_name("Social Impact")
    assert not is_plausible_person_name("Opportunity Champion")
    assert not is_plausible_person_name("Annual Highlights")
    assert not is_plausible_person_name("Community Grants")
    assert not is_plausible_person_name("Sustainability Goals")
    assert not is_plausible_person_name("Climate Commitments")
    assert not is_plausible_person_name("Platform Overview")

