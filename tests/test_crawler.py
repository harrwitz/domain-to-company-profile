import pytest
from crawler import (
    score_link,
    extract_candidate_links,
    extract_linkedin_links_from_html,
)
from utils import normalize_domain, is_same_domain, resolve_url

def test_normalize_domain():
    assert normalize_domain("https://www.postman.com/about/") == "postman.com"
    assert normalize_domain("http://supabase.com") == "supabase.com"
    assert normalize_domain("vapi.ai") == "vapi.ai"

def test_is_same_domain():
    assert is_same_domain("https://postman.com/company/about", "postman.com") is True
    assert is_same_domain("https://www.postman.com/company/about", "postman.com") is True
    assert is_same_domain("https://github.com/postman", "postman.com") is False

def test_score_link():
    # Priority keywords should score high (>=28)
    assert score_link("https://postman.com/company/about-postman/", "About Postman") >= 28
    assert score_link("https://supabase.com/company", "Company") >= 28
    assert score_link("https://vapi.ai/pricing", "Pricing & Plans") >= 8
    
    # Penalized links
    assert score_link("https://postman.com/login", "Login") < 0
    assert score_link("https://postman.com/logo.png", "Logo") < 0

def test_extract_linkedin_links_from_html():
    html = """
    <div class="card">
        <h3>Jane Doe — Founder</h3>
        <a href="https://www.linkedin.com/in/janedoe?ref=123">LinkedIn Profile</a>
    </div>
    """
    links = extract_linkedin_links_from_html(html, "https://example.com/team")
    assert len(links) == 1
    assert links[0]["url"] == "https://www.linkedin.com/in/janedoe"
    assert links[0]["source_page"] == "https://example.com/team"


def test_generalized_leadership_signals_score_higher_than_unrelated():
    """Verify that synthetic pages with strong leadership/team/founder signals score higher than unrelated pages."""
    from crawler import evaluate_link_relevance

    synthetic_base = "https://example-saas-platform.org"

    lead_score, lead_reason, is_lead, _, _ = evaluate_link_relevance(f"{synthetic_base}/leadership", "Leadership Team")
    exec_score, exec_reason, is_exec, _, _ = evaluate_link_relevance(f"{synthetic_base}/executives", "Our Executives")
    founder_score, founder_reason, is_founder, _, _ = evaluate_link_relevance(f"{synthetic_base}/our-story", "Letter From Our Founders")
    people_score, people_reason, is_people, _, _ = evaluate_link_relevance(f"{synthetic_base}/meet-the-team", "Who We Are")

    # Unrelated or utility pages
    docs_score, _, is_docs, _, _ = evaluate_link_relevance(f"{synthetic_base}/docs/api-reference", "API Documentation")
    features_score, _, is_features, _, _ = evaluate_link_relevance(f"{synthetic_base}/features/analytics", "Analytics Features")
    catalog_score, _, is_catalog, _, _ = evaluate_link_relevance(f"{synthetic_base}/products/catalog", "View Products")

    # Positive leadership signals must score higher
    assert lead_score >= 30 and is_lead is True
    assert exec_score >= 30 and is_exec is True
    assert founder_score >= 30 and is_founder is True
    assert people_score >= 30 and is_people is True

    # Generic technical/product pages must score lower and not be flagged as leadership
    assert docs_score <= 8 and is_docs is False
    assert features_score <= 8 and is_features is False
    assert catalog_score <= 8 and is_catalog is False

    # Direct comparison
    assert lead_score > docs_score
    assert exec_score > features_score
    assert founder_score > catalog_score


def test_surrounding_context_elevates_neutral_anchor_links():
    """Verify that neutral links (<a href="...">Read More</a>) receive high relevance when surrounded by leadership signals."""
    domain = "synthetic-corp.io"
    base_url = f"https://{domain}"
    html = f"""
    <html>
        <body>
            <div class="executive-card">
                <h3>Jane Doe</h3>
                <p>Chief Executive Officer & Co-founder</p>
                <a href="{base_url}/people/jane-doe">Read More</a>
            </div>
            <div class="product-card">
                <h3>Cloud Storage Module</h3>
                <p>High throughput storage clusters</p>
                <a href="{base_url}/products/cloud-storage">Read More</a>
            </div>
        </body>
    </html>
    """
    candidates = extract_candidate_links(html, base_url, domain, base_url)
    cand_by_url = {c.url: c for c in candidates}

    jane_cand = cand_by_url.get(f"{base_url}/people/jane-doe")
    product_cand = cand_by_url.get(f"{base_url}/products/cloud-storage")

    assert jane_cand is not None
    assert product_cand is not None

    # Jane's link should be elevated by the surrounding "Chief Executive Officer & Co-founder" text
    assert jane_cand.score >= 20
    assert "ounder" in jane_cand.reason or "eadership" in jane_cand.reason or "xecutive" in jane_cand.reason

    # The product link should NOT be leadership relevant and should score much lower
    assert product_cand.is_leadership_relevant is False
    assert product_cand.score < jane_cand.score


def test_parent_heading_elevates_link_relevance():
    """Verify that parent/preceding headings elevate links located under team/leadership sections."""
    domain = "enterprise-domain.net"
    base_url = f"https://{domain}"
    html = f"""
    <div>
        <h2>Company Leadership and Board of Directors</h2>
        <ul>
            <li><a href="{base_url}/governance/advisors">Advisory Board Directory</a></li>
        </ul>
        <h2>System Status & Reliability</h2>
        <ul>
            <li><a href="{base_url}/status">Uptime Metrics</a></li>
        </ul>
    </div>
    """
    candidates = extract_candidate_links(html, base_url, domain, base_url)
    cand_by_url = {c.url: c for c in candidates}

    advisors_cand = cand_by_url.get(f"{base_url}/governance/advisors")
    assert advisors_cand is not None
    assert advisors_cand.score >= 20

    # Status link should be penalized
    status_cand = cand_by_url.get(f"{base_url}/status")
    assert status_cand is None or status_cand.score < 0


def test_candidate_link_namedtuple_backward_compatibility():
    """Verify that CandidateLink retains 2-tuple unpacking compatibility."""
    from crawler import CandidateLink

    cand = CandidateLink(
        url="https://example.org/team",
        score=35,
        reason="Leadership signal in link",
        is_leadership_relevant=True,
        anchor_text="Our Team",
    )
    url, score = cand[:2]
    assert url == "https://example.org/team"
    assert score == 35
    assert cand.is_leadership_relevant is True
    assert cand.reason == "Leadership signal in link"


# ── New tests: word-boundary matching and product-compound detection ──────────

def test_word_boundary_prevents_substring_false_positives():
    """Short executive acronyms (cto, cro, coo, vp) must NOT match as substrings of unrelated words."""
    from crawler import evaluate_link_relevance

    base = "https://any-saas-company.com"

    # These should NOT trigger leadership signals
    vector_score, _, vector_lead, _, neg = evaluate_link_relevance(f"{base}/modules/vector", "Vector Database")
    assert vector_lead is False, "'cto' should not match inside 'vector'"
    assert vector_score < 15

    cron_score, _, cron_lead, _, _ = evaluate_link_relevance(f"{base}/modules/cron", "Cron Jobs")
    assert cron_lead is False, "'cro' should not match inside 'cron'"
    assert cron_score < 15

    cookie_score, _, cookie_lead, _, _ = evaluate_link_relevance(f"{base}/cookie-settings", "Cookie Settings")
    assert cookie_lead is False, "'coo' should not match inside 'cookie'"

    # But standalone executive acronyms SHOULD match
    cto_score, _, cto_lead, _, _ = evaluate_link_relevance(f"{base}/team/cto", "Our CTO")
    assert cto_score >= 30
    assert cto_lead is True


def test_product_compound_paths_not_leadership():
    """Leadership keywords in product compound paths (money-management, bot-management) must not be leadership-relevant."""
    from crawler import evaluate_link_relevance

    base = "https://fintech-platform.com"

    # Product compounds — NOT leadership pages
    mm_score, _, mm_lead, _, neg = evaluate_link_relevance(f"{base}/money-management/availability", "Money Management")
    assert mm_lead is False, "money-management is a product, not a leadership page"
    assert mm_score <= 10

    bm_score, _, bm_lead, _, _ = evaluate_link_relevance(f"{base}/security/bot-management", "Bot Management")
    assert bm_lead is False, "bot-management is a security feature, not leadership"
    assert bm_score <= 10

    # Leadership compounds — SHOULD be leadership pages
    mt_score, _, mt_lead, _, _ = evaluate_link_relevance(f"{base}/management-team", "Management Team")
    assert mt_lead is True, "management-team is about people"
    assert mt_score >= 30

    ol_score, _, ol_lead, _, _ = evaluate_link_relevance(f"{base}/our-leadership", "Our Leadership")
    assert ol_lead is True, "our-leadership is about people"
    assert ol_score >= 30


def test_surrounding_context_does_not_set_leadership_relevant():
    """Keywords found ONLY in surrounding text should boost score but NOT set is_leadership_relevant."""
    from crawler import evaluate_link_relevance

    base = "https://generic-company.com"

    # Leadership keyword only in surrounding context, not in path/anchor
    score, reason, is_lead, pos, neg = evaluate_link_relevance(
        f"{base}/some-page",
        anchor_text="Read More",
        surrounding_text="Our CEO discusses the future of AI",
    )
    assert is_lead is False, "CEO only in surrounding text should not set is_leadership_relevant"
    assert score >= 10, "Should still get a score boost from surrounding context"


def test_positive_and_negative_signals_returned():
    """evaluate_link_relevance must return meaningful positive and negative signal lists."""
    from crawler import evaluate_link_relevance

    base = "https://signals-test.com"

    # Leadership page — should have positive signals
    _, _, _, pos, neg = evaluate_link_relevance(f"{base}/leadership", "Leadership Team")
    assert len(pos) > 0, "Dedicated leadership page should have positive signals"
    assert any("leadership" in s for s in pos)

    # Product subpath with leadership keyword — should have negative signals
    _, _, _, pos2, neg2 = evaluate_link_relevance(f"{base}/security/bot-management", "Bot Management")
    assert len(neg2) > 0, "Leadership keyword in product subpath should have negative signals"


def test_company_about_pages_not_leadership_relevant():
    """Company/about pages are useful to crawl but should NOT be flagged as leadership-relevant."""
    from crawler import evaluate_link_relevance

    base = "https://generic-startup.io"

    score, _, is_lead, _, _ = evaluate_link_relevance(f"{base}/about", "About Us")
    assert score >= 20, "About page should have a decent crawl score"
    assert is_lead is False, "About page alone is not specifically about people/leadership"

    score2, _, is_lead2, _, _ = evaluate_link_relevance(f"{base}/company", "Company")
    assert score2 >= 20
    assert is_lead2 is False, "Company page alone is not specifically about people/leadership"


def test_customer_stories_not_leadership():
    """Customer story pages should not be flagged as leadership-relevant even if they mention executive titles."""
    from crawler import evaluate_link_relevance

    base = "https://enterprise-saas.com"

    score, _, is_lead, _, neg = evaluate_link_relevance(
        f"{base}/customer-stories/acme-corp",
        anchor_text="Acme Corp Success Story",
        surrounding_text="CEO of Acme Corp explains how they use our product",
    )
    assert is_lead is False, "Customer stories should not be leadership-relevant"
    assert score <= 10


def test_dedicated_team_page_high_confidence():
    """A /team or /people page should have high score AND is_leadership_relevant=True."""
    from crawler import evaluate_link_relevance

    base = "https://any-company.com"

    team_score, _, team_lead, pos, _ = evaluate_link_relevance(f"{base}/team", "Our Team")
    assert team_lead is True
    assert team_score >= 30
    assert any("people" in s or "team" in s for s in pos)

    people_score, _, people_lead, pos2, _ = evaluate_link_relevance(f"{base}/people", "People")
    assert people_lead is True
    assert people_score >= 30


def test_article_roots_receive_discovery_priority_and_outrank_customer_case_studies():
    """Verify that corporate /blog, /news, /press roots receive meaningful priority and outrank customer case studies."""
    from crawler import evaluate_link_relevance

    base = "https://example-saas.com"

    # Corporate article/news roots
    blog_score, _, blog_lead, blog_pos, _ = evaluate_link_relevance(f"{base}/blog", anchor_text="Blog")
    news_score, _, news_lead, news_pos, _ = evaluate_link_relevance(f"{base}/news", anchor_text="News")
    press_score, _, press_lead, press_pos, _ = evaluate_link_relevance(f"{base}/press", anchor_text="Press")
    newsroom_score, _, newsroom_lead, _, _ = evaluate_link_relevance(f"{base}/newsroom", anchor_text="Newsroom")
    insights_score, _, insights_lead, _, _ = evaluate_link_relevance(f"{base}/insights", anchor_text="Insights")

    # 1. Corporate /blog root receives meaningful discovery priority (>= 22)
    assert blog_score >= 22
    assert any("article-root" in s for s in blog_pos)
    assert news_score >= 22
    assert press_score >= 22
    assert newsroom_score >= 22
    assert insights_score >= 22

    # 2. /blog is NOT itself marked as leadership merely because it is an article root
    assert blog_lead is False
    assert news_lead is False
    assert press_lead is False

    # 3. Customer case studies do NOT outrank corporate article roots, even with incidental team/exec keywords
    customer_score1, _, cust_lead1, _, _ = evaluate_link_relevance(
        f"{base}/customers/acme-corp",
        anchor_text="How Acme Corp uses our platform",
        surrounding_text="Loved by engineering teams worldwide",
    )
    customer_score2, _, cust_lead2, _, _ = evaluate_link_relevance(
        f"{base}/customer-stories/startup-x",
        anchor_text="CEO of Startup X speaks",
        surrounding_text="Empowering product staff",
    )
    assert cust_lead1 is False
    assert cust_lead2 is False
    assert customer_score1 <= 8
    assert customer_score2 <= 8
    assert blog_score > customer_score1
    assert blog_score > customer_score2


def test_dedicated_leadership_and_platform_ugc_isolation():
    """Verify that /about/leadership remains a dedicated leadership page and platform UGC remains excluded."""
    from crawler import evaluate_link_relevance

    base = "https://enterprise-code-host.com"

    # 4. /about/leadership remains a dedicated leadership page
    lead_score, _, is_lead, pos, _ = evaluate_link_relevance(
        f"{base}/about/leadership",
        anchor_text="Leadership",
    )
    assert is_lead is True
    assert lead_score >= 35
    assert any("leadership" in s for s in pos)

    # 5. Platform/user-generated URLs remain excluded
    ugc_score1, _, ugc_lead1, _, _ = evaluate_link_relevance(f"{base}/Leadership/bot-script")
    ugc_score2, _, ugc_lead2, _, _ = evaluate_link_relevance(f"{base}/org/repo/commits")
    ugc_score3, _, ugc_lead3, _, _ = evaluate_link_relevance(f"{base}/workspace/user-project")

    assert ugc_score1 < 0
    assert ugc_lead1 is False
    assert ugc_score2 < 0
    assert ugc_lead2 is False
    assert ugc_score3 < 0
    assert ugc_lead3 is False

    # Also verify content inspection excludes user profiles
    from crawler import _inspect_page_content_identity
    mock_profile_html = (
        '<html><head><meta property="og:type" content="profile">'
        '<title>akazemi10 (Ali Kazemi) · GitHub</title></head>'
        '<body><div>pinned repositories and contributions in the last year</div>'
        '<p>Detailed developer profile workspace content to ensure buffer is over two hundred characters.</p></body></html>'
    )
    identity = _inspect_page_content_identity(mock_profile_html, f"{base}/akazemi10", f"{base}/akazemi10")
    assert identity == "platform_ugc"

