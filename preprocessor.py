"""
preprocessor.py — HTML → Clean Markdown Pipeline

Converts raw HTML pages into clean, token-efficient markdown suitable for
LLM consumption. This is the critical cost-optimization layer — raw HTML
can be 50-200K tokens per page, but clean markdown is typically 2-5K tokens.

Pipeline:
  1. BeautifulSoup: Strip script, style, SVG, nav, footer, etc.
  2. Trafilatura: Intelligent boilerplate removal (ads, sidebars, banners)
  3. Markdownify: Convert cleaned HTML → structured Markdown
  4. Token budgeting: Truncate to per-page and per-domain limits
  5. Email extraction: Regex-based discovery from raw HTML
"""

from bs4 import BeautifulSoup, Comment
from markdownify import markdownify as md
import trafilatura

from config import (
    STRIP_TAGS,
    MAX_TOKENS_PER_PAGE,
    MAX_TOKENS_TOTAL,
    CHARS_PER_TOKEN,
)
from utils import (
    logger,
    extract_emails_from_text,
    truncate_text,
)
from search_fallback import normalize_org_name, metrics
from models import CrawlStats, LeadershipCandidate, CandidateAffiliation


def safe_get_attr(tag, key: str, default: str = "") -> str:
    """Safely get an attribute from a BeautifulSoup tag, handling None attrs, missing attrs, or non-Tag elements."""
    if tag is None:
        return default
    attrs = getattr(tag, "attrs", None)
    if not isinstance(attrs, dict):
        return default
    val = attrs.get(key, default)
    if val is None:
        return default
    if isinstance(val, list):
        return " ".join(str(v) for v in val)
    return str(val)


def strip_unwanted_tags(html: str) -> str:
    """Remove script, style, SVG, nav, and other boilerplate tags from HTML."""
    if not html or not isinstance(html, str):
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # Remove specified tags entirely
    for tag_name in STRIP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Remove hidden elements safely
    for tag in soup.find_all(attrs={"style": True}):
        style = safe_get_attr(tag, "style")
        if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
            tag.decompose()

    # Remove aria-hidden elements (often decorative)
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()

    return str(soup)


def extract_with_trafilatura(html: str) -> str | None:
    """Use Trafilatura for intelligent main content extraction.

    Trafilatura excels at removing boilerplate (cookie banners, ads, sidebars)
    while preserving the main article/page content.
    """
    try:
        extracted = trafilatura.extract(
            html,
            output_format="txt",
            include_links=True,
            include_tables=True,
            favor_recall=True,  # Prefer getting more content over precision
        )
        return extracted
    except Exception as e:
        logger.debug(f"    Trafilatura extraction failed: {e}")
        return None


def html_to_markdown(html: str) -> str:
    """Convert cleaned HTML to structured Markdown using markdownify."""
    try:
        markdown = md(
            html,
            heading_style="ATX",       # Use # for headings
            bullets="-",               # Use - for lists
            strip=["img", "picture", "video", "audio", "source", "canvas"],
        )
        # Clean up excessive whitespace
        import re
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        markdown = re.sub(r" {2,}", " ", markdown)
        return markdown.strip()
    except Exception as e:
        logger.debug(f"    Markdownify conversion failed: {e}")
        return ""


def preprocess_page(url: str, html: str) -> tuple[str, list[str]]:
    """Process a single page's HTML into clean markdown + extracted emails.

    Args:
        url: The page URL (for logging context)
        html: Raw rendered HTML content

    Returns:
        Tuple of (clean_markdown, list_of_emails)
    """
    # Step 0: Extract emails from raw HTML before stripping anything
    emails = extract_emails_from_text(html)

    # Step 1: Strip unwanted tags
    cleaned_html = strip_unwanted_tags(html)

    # Step 2: Try Trafilatura for main content extraction
    trafilatura_text = extract_with_trafilatura(html)

    # Step 3: Convert to markdown
    markdown_text = html_to_markdown(cleaned_html)

    # Step 4: Choose the better result
    # Trafilatura is better at removing boilerplate; markdownify preserves structure.
    # Use trafilatura if it produced meaningful content, otherwise fall back to markdownify.
    if trafilatura_text and len(trafilatura_text) > 100:
        # Combine: Use trafilatura for main content, but check if markdownify
        # captured important structured data (tables, lists) that trafilatura missed
        final_text = trafilatura_text

        # If markdownify captured significantly more content, append key sections
        if len(markdown_text) > len(trafilatura_text) * 1.5:
            # Append markdownify content that might have additional structured data
            final_text = f"{trafilatura_text}\n\n---\n\n{markdown_text}"
    elif markdown_text:
        final_text = markdown_text
    else:
        # Last resort: extract just the text
        soup = BeautifulSoup(cleaned_html, "html.parser")
        final_text = soup.get_text(separator="\n", strip=True)

    # Step 5: Truncate to per-page token budget
    max_chars = MAX_TOKENS_PER_PAGE * CHARS_PER_TOKEN
    final_text = truncate_text(final_text, max_chars)

    return final_text, emails


import re

def extract_people_cards_from_html(html: str) -> list[str]:
    """Inspect DOM for people cards, bio blocks, and team containers.

    Preserves structured candidate evidence before boilerplate removal.
    Explicitly ignores investor lists, customer stories, and testimonials.
    """
    soup = BeautifulSoup(html, "html.parser")
    card_snippets: list[str] = []
    seen = set()

    # Look for explicit team cards, bios, or team member lists (NEVER investor lists or testimonials)
    cards = soup.find_all(
        ["div", "li", "article", "section", "figcaption", "tr"],
        class_=lambda c: c and any(k in str(c).lower() for k in ["team", "people", "member", "founder", "leader", "executive", "bio"]) and not any(inv in str(c).lower() for inv in ["investor", "testimonial", "customer-stories", "quote"])
    )

    for card in cards:
        text = card.get_text(" ", strip=True)
        if len(text) < 15 or len(text) > 400:
            continue

        # Skip if card text contains investor or customer testimonial context
        if any(inv in text.lower() for inv in ["investor", "individual investor", "our investors", "testimonial", "customer stories"]):
            continue

        # Look for LinkedIn profile links in card
        lk_urls = []
        for a in card.find_all("a", href=True):
            if "linkedin.com/in/" in a["href"].lower() or "linkedin.com/pub/" in a["href"].lower():
                lk_urls.append(a["href"].split("?")[0].rstrip("/"))

        # If card has role/title keywords or LinkedIn link, preserve snippet
        if lk_urls or any(r in text.lower() for r in ["ceo", "cto", "co-founder", "cofounder", "founder", "vp ", "head of", "director", "president", "devrel"]):
            snippet = text
            if lk_urls:
                snippet += f" | LinkedIn: {', '.join(lk_urls)}"
            if snippet not in seen:
                seen.add(snippet)
                card_snippets.append(snippet)

    return card_snippets


def preprocess_page(url: str, html: str) -> tuple[str, list[str]]:
    """Process a single page's HTML into clean markdown + extracted emails.

    Args:
        url: The page URL (for logging context)
        html: Raw rendered HTML content

    Returns:
        Tuple of (clean_markdown, list_of_emails)
    """
    if not html or not isinstance(html, str) or not html.strip():
        return "", []

    # Step 0: Extract emails from raw HTML before stripping anything
    emails = extract_emails_from_text(html)

    # Step 1: Extract preserved DOM people cards before stripping HTML tags
    people_cards = extract_people_cards_from_html(html)

    # Step 2: Strip unwanted tags
    cleaned_html = strip_unwanted_tags(html)

    # Step 3: Try Trafilatura for main content extraction
    trafilatura_text = extract_with_trafilatura(html)

    # Step 4: Convert to markdown
    markdown_text = html_to_markdown(cleaned_html)

    # Step 5: Choose the best content representation
    if trafilatura_text and len(trafilatura_text) > 100:
        final_text = trafilatura_text
        if len(markdown_text) > len(trafilatura_text) * 1.5:
            final_text = f"{trafilatura_text}\n\n---\n\n{markdown_text}"
    elif markdown_text:
        final_text = markdown_text
    else:
        soup = BeautifulSoup(cleaned_html, "html.parser")
        final_text = soup.get_text(separator="\n", strip=True)

    # Prepend preserved DOM people cards if present
    if people_cards:
        cards_block = "### PRESERVED PEOPLE & TEAM CARDS:\n" + "\n".join(f"- {c}" for c in people_cards[:20])
        final_text = f"{cards_block}\n\n{final_text}"

    # Step 6: Truncate to per-page token budget
    max_chars = MAX_TOKENS_PER_PAGE * CHARS_PER_TOKEN
    final_text = truncate_text(final_text, max_chars)

    return final_text, emails


from models import LeadershipCandidate, CandidateAffiliation
import json


class EvidenceCategory:
    """Standardized evidence categories distinguishing first-party from third-party."""
    FIRST_PARTY_LEADERSHIP = "FIRST_PARTY_LEADERSHIP"
    FIRST_PARTY_EMPLOYEE = "FIRST_PARTY_EMPLOYEE"
    FIRST_PARTY_COMPANY_ANNOUNCEMENT = "FIRST_PARTY_COMPANY_ANNOUNCEMENT"
    FIRST_PARTY_AUTHOR_BIO = "FIRST_PARTY_AUTHOR_BIO"
    CUSTOMER_STORY = "CUSTOMER_STORY"
    TESTIMONIAL = "TESTIMONIAL"
    PARTNER_PAGE = "PARTNER_PAGE"
    EXTERNAL_COMPANY_REFERENCE = "EXTERNAL_COMPANY_REFERENCE"
    INVESTOR = "INVESTOR"
    AMBIGUOUS = "AMBIGUOUS"


def classify_page_type(url: str) -> str:
    """Classify page category from URL."""
    url_lower = url.lower()
    if any(k in url_lower for k in ["/about", "/team", "/leadership", "/company", "/people", "/founders", "/executives", "/our-team"]):
        return "team"
    if any(k in url_lower for k in ["/customer", "/case-study", "/case_study", "/story", "/stories", "/partner", "/clients", "/testimonial"]):
        return "customer"
    if any(k in url_lower for k in ["/blog", "/news", "/press", "/media"]):
        return "press"
    if any(k in url_lower for k in ["/pricing", "/sales", "/contact"]):
        return "business"
    return "general"


COMPOUND_ROLE_REGEX = (
    r"\b("
    r"Co-Founder\s*(?:and|&|\/)\s*CEO|CEO\s*(?:and|&|\/)\s*Co-Founder|"
    r"Co-Founder\s*(?:and|&|\/)\s*CTO|CTO\s*(?:and|&|\/)\s*Co-Founder|"
    r"Founder\s*(?:and|&|\/)\s*CEO|CEO\s*(?:and|&|\/)\s*Founder|"
    r"Founder\s*(?:and|&|\/)\s*CTO|CTO\s*(?:and|&|\/)\s*Founder|"
    r"Product\s+Architect\s*(?:\/|and|&)\s*Co-Founder|Founding\s+Engineer|"
    r"Chief\s+[A-Z][a-z]+\s+Officer|"
    r"VP\s+(?:of\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|Vice\s+President|"
    r"Director\s+(?:of\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|"
    r"Head\s+of\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|"
    r"Principal\s+(?:Engineer|Architect|Designer|Scientist|Developer)|"
    r"Lead\s+(?:Engineer|Architect|Designer|Developer|Product|Data|DevRel)|"
    r"CEO|CTO|COO|CFO|CMO|CRO|CPO|Co-Founder|Cofounder|"
    r"Founder(?!\s+(?:mindset|mentality|equity|shares|stock|story|journey|perspective|vision|culture|mode))|"
    r"President|DevRel"
    r")\b"
)

COMMON_ROLE_TITLE_WORDS = {
    "ceo", "cto", "cpo", "coo", "cro", "founder", "co-founder", "cofounder",
    "president", "director", "officer", "chief", "executive", "manager", "head",
    "lead", "architect", "engineer", "devrel", "board", "leadership",
    "technology", "product", "marketing", "operating", "revenue", "financial",
    "team", "member", "author", "speaker", "guest", "customer", "partner"
}

CTA_ACTION_VERBS = {
    "explore", "discover", "learn", "read", "view", "download", "browse", "find",
    "get", "visit", "watch", "check", "subscribe", "see", "start", "try", "join",
    "build", "share", "follow", "click", "open", "dive", "listen"
}

NON_PERSON_WORDS = {
    "announces", "launch", "release", "releases", "updates", "solutions", "platform", "product",
    "architect", "engineer", "developer", "developers", "software", "service", "services", "management", "security",
    "company", "overview", "features", "pricing", "support",
    "customer", "partner", "partners", "integration", "integrations", "community", "enterprise", "documentation", "lifecycle",
    "terms", "privacy", "cookie", "policy", "sign", "login", "register", "join", "get", "started",
    "devrel", "individual", "investors", "stories", "testimonials", "million", "billion", "thousand",
    "series", "round", "funding", "seed", "capital", "tour", "event", "summit", "conference", "galaxy",
    "awards", "report", "guide", "whitepaper", "webinar", "press", "media", "news", "announcement",
    "hackathon", "contestants", "contestant", "speaker", "speakers", "pipeline", "pipelines",
    "storage", "database", "databases", "realtime", "auth", "functions", "vector", "compliance",
    "regions", "availability", "postgres", "prototype", "backup", "backups", "recovery", "catalog",
    "schema", "instructions", "workflow", "architecture", "scale", "scaling", "options", "audit",
    "router", "framework", "library", "client", "sdk", "api", "apis",
    "kaizen", "mindset", "weeks", "days", "months", "years", "hours", "custard", "creams",
    "hire", "hiring", "culture", "mission", "vision", "values", "extreme", "egoless", "default",
    "ambition", "strategy", "sprint", "agile", "scrum", "who", "we", "they", "you", "them", "us",
    "impact", "initiative", "initiatives", "program", "programs", "champion", "champions",
    "opportunity", "opportunities", "diversity", "inclusion", "equity", "sustainability",
    "dive", "spotlight", "showcase", "commitments", "pledge", "fellowship", "foundation",
    "grants", "giving", "highlights", "recap", "annual", "insights", "resources", "benchmarks",
    "social", "civic", "public", "environmental", "global", "case", "study", "studies",
    "responsibility", "governance", "climate", "ecosystem"
}

STOP_NAME_WORDS = {
    "the", "our", "your", "their", "its", "this", "that", "all", "meet", "join",
    "follow", "about", "founders", "founder", "team", "leadership", "leaders",
    "executives", "executive", "members", "member", "people", "investors", "investor",
    "advisors", "advisor", "customers", "customer", "partners", "partner", "stories", "story",
    "article", "podcast", "press", "news", "blog", "contact", "pricing", "careers", "jobs",
    "who", "we", "they", "you", "us", "hire", "hiring", "how", "what", "why", "when"
}

ENGLISH_PRONOUNS_AND_DETERMINERS = {
    "his", "her", "their", "our", "my", "your", "its", "the", "a", "an",
    "this", "that", "these", "those", "each", "every", "all", "some", "any",
    "which", "what", "who", "whom", "whose", "where", "when", "why", "how",
    "other", "another", "such", "first", "last", "new", "next", "one", "two",
    "three", "many", "several", "most", "few"
}


def is_plausible_person_name(name: str, target_org_norm: str = "") -> bool:
    """Validate that candidate name is a plausible human name and not a phrase or concept.

    Requires:
      - Exactly 2 or 3 words
      - Each word starts with an uppercase letter
      - First word is not an imperative CTA verb (e.g. 'Explore', 'Discover', 'Read')
      - No grammatical function words, verbs, pronouns, or corporate/tech concept words
      - Does not match target company name
    """
    if not name or not isinstance(name, str):
        return False
    parts = [p.strip(" ,.-") for p in name.split() if p.strip(" ,.-")]
    if len(parts) not in [2, 3]:
        return False
    if parts[0].lower() in CTA_ACTION_VERBS:
        return False
    for i, p in enumerate(parts):
        if i == 1 and p.lower() in {"van", "de", "von", "der"}:
            continue
        subparts = p.split("-")
        if not all(sp and sp[0].isupper() and sp[1:].isalpha() for sp in subparts):
            return False
        p_lower = p.lower()
        if target_org_norm and p_lower == target_org_norm:
            return False
        if (
            p_lower in NON_PERSON_WORDS
            or p_lower in COMMON_ROLE_TITLE_WORDS
            or p_lower in ENGLISH_PRONOUNS_AND_DETERMINERS
            or p_lower in STOP_NAME_WORDS
            or p_lower in CTA_ACTION_VERBS
        ):
            return False
    return True


def disambiguate_name_and_company(raw_name: str, context_text: str) -> tuple[str, str | None]:
    """Separate pure person name from an associated company without hardcoding company lists.

    Handles:
      - 'Cassidy Williams Netlify' (where Netlify is followed by DevRel/role or is a corporate entity)
      - 'Alex Solomon PagerDuty' (where PagerDuty is followed by Cofounder/role)
      - 'Name — Company' or 'Name (Company)'
      - Multi-part Dutch/German names like 'Kevin van Gundy' (preserved as 3-word person name)
    """
    if not raw_name:
        return "", None

    parts = [p.strip(" ,.-") for p in raw_name.split() if p.strip(" ,.-")]
    if len(parts) < 2:
        return "", None

    associated_company = None
    name_candidate = " ".join(parts)

    if len(parts) >= 3:
        last_word = parts[-1]
        if parts[1].lower() not in {"van", "de", "von", "der"}:
            if last_word in ["Co", "Inc", "LLC", "Corp", "Ltd", "Labs", "AI", "HQ", "Technologies"]:
                associated_company = last_word
                name_candidate = " ".join(parts[:-1])
            else:
                text_lower = context_text.lower()
                role_keywords = [
                    "ceo", "cto", "cpo", "coo", "cro", "cfo", "founder", "cofounder",
                    "co-founder", "vp", "lead", "devrel", "director", "architect", "president"
                ]
                last_lower = last_word.lower()
                if any(
                    f"{last_lower} {rk}" in text_lower
                    or f"{last_lower}, {rk}" in text_lower
                    or f"{last_lower} — {rk}" in text_lower
                    or f"{last_lower} - {rk}" in text_lower
                    for rk in role_keywords
                ):
                    associated_company = last_word
                    name_candidate = " ".join(parts[:-1])

    if name_candidate.endswith(" Co"):
        name_candidate = name_candidate[:-3].strip()

    return name_candidate, associated_company


def extract_org_from_text(text: str) -> list[str]:
    """Extract organization names associated with a person using generic patterns."""
    patterns = [
        r"(?i:\b(?:CEO|CTO|CPO|COO|CRO|Co-Founder|Cofounder|Founder|VP|President|Director|Head)\s+(?:at|of|for|from|on|@)\s+)([A-Z][A-Za-z0-9\.\-\/]+(?:\s+[A-Z][A-Za-z0-9\.\-]+)?)\b",
        r"\b([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+)?)\s+(?i:(?:CEO|CTO|CPO|COO|CRO|Founder|Customer|Partner|Executive|DevRel))\b",
        r"(?:—|-|\||\()\s*([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+)?)\s*(?:\)|$)",
    ]
    orgs = []
    for pat in patterns:
        matches = re.findall(pat, text)
        for m in matches:
            if isinstance(m, tuple):
                m = m[0]
            m_clean = m.strip(" .,()")
            words = [w.lower() for w in m_clean.split()]
            if any(
                w in COMMON_ROLE_TITLE_WORDS
                or w in ENGLISH_PRONOUNS_AND_DETERMINERS
                or w in NON_PERSON_WORDS
                or w in {"and", "or", "the", "our", "a", "an", "this", "that", "their", "said", "announced"}
                for w in words
            ):
                continue
            if m_clean and len(m_clean) > 2:
                orgs.append(m_clean)
    return list(dict.fromkeys(orgs))


def get_nearest_heading(element) -> str:
    """Find the nearest true section heading text above or enclosing an element."""
    if not element:
        return ""

    # 1. Check parent section/article/div attributes for section hints
    curr = element
    while curr:
        classes = safe_get_attr(curr, "class").lower()
        curr_id = safe_get_attr(curr, "id").lower()
        if any(inv in classes or inv in curr_id for inv in ["investor", "testimonial", "customer", "quote", "partner", "press", "podcast"]):
            if "investor" in classes or "investor" in curr_id:
                return "Individual investors"
            if "testimonial" in classes or "customer" in classes or "quote" in classes:
                return "Customer Testimonials"
            if "partner" in classes:
                return "Our Partners"
        curr = getattr(curr, "parent", None)

    # 2. Look backwards for nearest heading that is an actual section heading
    curr = element
    while curr:
        if hasattr(curr, "find_all_previous"):
            headings = curr.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
            for h in headings[:25]:
                h_text = h.get_text(" ", strip=True)
                if not h_text:
                    continue
                h_lower = h_text.lower()
                # If heading contains section keywords, it's definitely a section heading
                if any(k in h_lower for k in [
                    "team", "leadership", "founder", "executive", "investor", "customer",
                    "story", "stories", "testimonial", "press", "partner", "about", "board",
                    "management", "people", "who we are", "what our", "backed", "angel",
                    "individual investors"
                ]):
                    return h_text
        curr = getattr(curr, "parent", None)

    return ""


def parse_candidate_identity_and_employer(
    container,
    text: str,
    target_domain: str,
) -> tuple[str, str, str | None]:
    """Separate pure person name, role, and associated employer using DOM signals.

    Never concatenates company into name (e.g. 'Cassidy Williams Netlify' -> 'Cassidy Williams', 'Netlify').
    Returns (name, role, associated_company).
    """
    if container is None:
        return "", "", None

    target_norm = normalize_org_name(target_domain)

    container_classes = safe_get_attr(container, "class").lower()
    container_id = safe_get_attr(container, "id").lower()

    # Discard navigation, header, footer, sidebar, or pagination containers
    is_navigation = (
        container.name in ["nav", "header", "footer"]
        or any(k in container_classes for k in ["nav", "menu", "sidebar", "breadcrumb", "footer", "pagination"])
        or any(k in container_id for k in ["nav", "menu", "sidebar", "breadcrumb", "footer"])
        or any(parent.name in ["nav", "header", "footer"] for parent in getattr(container, "parents", []))
    )
    if is_navigation:
        return "", "", None

    # Discard containers specifically labeled as non-person marketing, reports, or initiatives
    is_non_person_container = any(k in container_classes for k in [
        "initiative", "program", "report", "metric", "stat", "feature", "product", "resource", "download", "cta"
    ]) or any(k in container_id for k in [
        "initiative", "program", "report", "metric", "stat", "feature", "product", "resource", "download", "cta"
    ])
    if is_non_person_container:
        return "", "", None

    # 1. Structural DOM check: check if container has heading/bold or explicit person card
    name_el = container.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"]) if hasattr(container, "find") else None
    has_linkedin = bool(container.find("a", href=lambda h: h and ("linkedin.com/in/" in h.lower() or "linkedin.com/pub/" in h.lower()))) if hasattr(container, "find") else False
    is_person_card = any(k in container_classes for k in [
        "team-member", "team_member", "teammember", "person", "profile", "bio", "speaker", "author",
        "leader", "founder", "executive", "byline", "contributor", "employee", "staff"
    ]) or bool(container.find(attrs={"itemtype": re.compile(r"schema\.org/Person", re.I)}))

    raw_name = ""
    if name_el:
        candidate_tag_text = name_el.get_text(" ", strip=True)
        tag_words = [w.lower() for w in candidate_tag_text.split()]
        if not any(w in STOP_NAME_WORDS or w in NON_PERSON_WORDS or w in COMMON_ROLE_TITLE_WORDS for w in tag_words):
            raw_name = candidate_tag_text

    # Clean raw_name from embedded roles
    if raw_name:
        for stop_p in ["CEO", "CTO", "DevRel", "Founder", "Cofounder", "Co-Founder", "CRO", "VP"]:
            if stop_p.lower() in raw_name.lower():
                raw_name = re.split(rf"\b{stop_p}\b", raw_name, flags=re.IGNORECASE)[0].strip()

    name_candidate = ""
    associated_company = None

    if raw_name:
        cleaned_raw, assoc = disambiguate_name_and_company(raw_name, text)
        if is_plausible_person_name(cleaned_raw, target_norm):
            name_candidate = cleaned_raw
            associated_company = assoc

    # Check for role in text first so we can use role position as an anchor
    role_match = re.search(COMPOUND_ROLE_REGEX, text, re.IGNORECASE)

    # 2. Check stripped strings in container (e.g. <div>Jane Doe\nCEO & Co-Founder</div>)
    if not name_candidate:
        for s in list(container.stripped_strings)[:6]:
            s_clean = s.strip()
            for stop_p in ["CEO", "CTO", "DevRel", "Founder", "Cofounder", "Co-Founder", "CRO", "VP"]:
                if stop_p.lower() in s_clean.lower():
                    s_clean = re.split(rf"\b{stop_p}\b", s_clean, flags=re.IGNORECASE)[0].strip()
            if "," in s_clean:
                s_clean = s_clean.split(",")[0].strip()
            if "—" in s_clean or " - " in s_clean:
                s_clean = re.split(r"\s*[—-]\s*", s_clean)[0].strip()
            cleaned_s, assoc = disambiguate_name_and_company(s_clean, text)
            if is_plausible_person_name(cleaned_s, target_norm):
                name_candidate = cleaned_s
                associated_company = assoc
                break

    # 3. Check role-adjacent name syntax (e.g. "Jane Doe, CEO", "Alice Miller, Chief Technology Officer", "Jane Doe, Founder")
    if not name_candidate and role_match:
        role_start = role_match.start()
        # Look backwards up to 60 characters before the role
        prefix = text[max(0, role_start - 60):role_start].strip(" ,—-|:\n\t")
        words = prefix.split()
        if len(words) >= 2:
            for n_words in [3, 2]:
                if len(words) >= n_words:
                    cand_str = " ".join(words[-n_words:])
                    cand_str = cand_str.strip(" ,—-|:\n\t\"'“”()")
                    cleaned_str, assoc = disambiguate_name_and_company(cand_str, text)
                    if is_plausible_person_name(cleaned_str, target_norm):
                        name_candidate = cleaned_str
                        associated_company = assoc
                        break
                if name_candidate:
                    break

    # 4. Check if role precedes the name (e.g. "CEO Jane Doe" or "Founder: Jane Doe")
    if not name_candidate and role_match:
        role_end = role_match.end()
        suffix = text[role_end:min(len(text), role_end + 60)].strip(" ,—-|:\n\t")
        words = suffix.split()
        if len(words) >= 2:
            for n_words in [2, 3]:
                if len(words) >= n_words:
                    cand_str = " ".join(words[:n_words])
                    cand_str = cand_str.strip(" ,—-|:\n\t\"'“”()")
                    cleaned_str, assoc = disambiguate_name_and_company(cand_str, text)
                    if is_plausible_person_name(cleaned_str, target_norm):
                        name_candidate = cleaned_str
                        associated_company = assoc
                        break
                if name_candidate:
                    break

    # 5. Only fall back to regex if container is an explicit structured person card or has LinkedIn
    if not name_candidate and (is_person_card or has_linkedin):
        matches = re.findall(r"\b([A-Z][a-z]+(?:\s+(?:van\s+|de\s+|von\s+)?[A-Z][a-z\-]+)+)\b", text)
        for m in matches:
            cleaned_m, assoc = disambiguate_name_and_company(m, text)
            if is_plausible_person_name(cleaned_m, target_norm):
                name_candidate = cleaned_m
                associated_company = assoc
                break

    if not name_candidate:
        return "", "", None

    has_explicit_role = bool(role_match)

    # Verify proximity between candidate name and role to prevent associating unrelated buzzwords
    if role_match and name_candidate:
        name_idx = text.find(name_candidate)
        role_idx = role_match.start()
        if name_idx != -1:
            span_between = text[min(name_idx + len(name_candidate), role_idx):max(name_idx, role_idx)]
            if len(span_between) > 60 or ("." in span_between and "\n" not in span_between):
                has_explicit_role = False
                role_match = None

    has_person_bio = bool(re.search(
        r"\b(?:joined\s+(?:in|as|the)|prior\s+to|before\s+joining|holds\s+a\s+(?:degree|bachelor|master|phd)|graduated\s+from|years\s+of\s+experience|previously\s+worked|serves\s+as|responsible\s+for)\b|"
        r"\b(?:he|she|his|her)\s+(?:leads|heads|manages|co-founded|founded|joined|is|has|studied)\b",
        text,
        re.IGNORECASE
    ))

    # A candidate must have an explicit role, a LinkedIn profile, or a verified person biography
    if not has_explicit_role and not has_linkedin and not has_person_bio:
        return "", "", None

    if role_match:
        role = role_match.group(1).strip()
    elif has_linkedin or has_person_bio:
        role = "Team Member"
    else:
        return "", "", None

    # Check target company possessive pattern: e.g. "Postman's CEO and co-founder"
    if target_norm and re.search(rf"\b{re.escape(target_norm)}'s\s+(?:CEO|CTO|CPO|COO|CRO|Founder|Co-Founder|President|VP|Head|Director)\b", text, re.IGNORECASE):
        associated_company = target_domain

    if not associated_company and role_match:
        role_start = role_match.start()
        pre_role = text[:role_start].strip()
        if name_candidate in pre_role:
            between = pre_role.split(name_candidate, 1)[1].strip(" ,—-|:\n\t")
            if between and len(between.split()) in [1, 2]:
                candidate_org = between.strip(" .,()")
                if (
                    candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS
                    and candidate_org.lower() not in NON_PERSON_WORDS
                    and candidate_org.lower() not in ENGLISH_PRONOUNS_AND_DETERMINERS
                ):
                    associated_company = candidate_org

    if not associated_company:
        at_match = re.search(r"(?i:\b(?:at|of|for|from|on|@)\s+)([A-Z][A-Za-z0-9\.\-]+)\b", text)
        if at_match:
            candidate_org = at_match.group(1).strip(" .,()")
            if (
                candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS
                and candidate_org.lower() not in NON_PERSON_WORDS
                and candidate_org.lower() not in ENGLISH_PRONOUNS_AND_DETERMINERS
            ):
                associated_company = candidate_org

    if not associated_company:
        dash_match = re.search(rf"{re.escape(name_candidate)}\s*(?:—|-|\|)\s*(?:{re.escape(role)}\s*)?([A-Z][A-Za-z0-9\.\-]+)", text)
        if dash_match:
            candidate_org = dash_match.group(1).strip(" .,()")
            if (
                candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS
                and candidate_org.lower() not in NON_PERSON_WORDS
                and candidate_org.lower() not in ENGLISH_PRONOUNS_AND_DETERMINERS
            ):
                associated_company = candidate_org

    if not associated_company:
        post_role_match = re.search(r",\s*([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][a-z0-9\.\-]+)?)\s*$", text)
        if post_role_match:
            candidate_org = post_role_match.group(1).strip(" .,()")
            if (
                candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS
                and candidate_org.lower() not in NON_PERSON_WORDS
                and candidate_org.lower() not in ENGLISH_PRONOUNS_AND_DETERMINERS
            ):
                associated_company = candidate_org

    return name_candidate, role, associated_company


def extract_candidate_evidence_from_page(
    page_url: str,
    html: str,
    target_domain: str,
    linkedin_links: list[dict] | None = None,
) -> list[LeadershipCandidate]:
    """Stage A: Discover candidate people & build explicit evidence objects."""
    if not html or not isinstance(html, str) or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")
    page_type = classify_page_type(page_url)
    target_org_norm = normalize_org_name(target_domain)

    candidates: list[LeadershipCandidate] = []
    seen_candidate_keys = set()

    linkedin_map = {}
    if linkedin_links:
        for item in linkedin_links:
            if isinstance(item, dict):
                ctx = item.get("context", "")
                url = item.get("url", "")
                if ctx and url:
                    linkedin_map[str(ctx).lower()] = str(url)

    # 1. JSON-LD BlogPosting / Article authors
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(s.string)
            if isinstance(ld, dict) and ld.get("@type") in ["BlogPosting", "Article"]:
                author_data = ld.get("author")
                author_list = author_data if isinstance(author_data, list) else ([author_data] if isinstance(author_data, dict) else [])
                for a in author_list:
                    aname = a.get("name") if isinstance(a, dict) else None
                    if aname and len(aname.split()) in [2, 3]:
                        cand_key = f"{aname.lower().strip()}::{page_url}"
                        if cand_key not in seen_candidate_keys:
                            seen_candidate_keys.add(cand_key)
                            role = "Author"
                            m = re.search(rf"{re.escape(aname)}\s+{COMPOUND_ROLE_REGEX}", soup.get_text(" ", strip=True), re.I)
                            if m:
                                role = m.group(1).strip()
                            has_author_exec = any(r in role.lower() for r in [
                                "ceo", "cto", "cpo", "coo", "cro", "cfo", "founder", "co-founder",
                                "cofounder", "president", "vp", "chief", "head of", "director",
                                "architect", "lead", "principal", "founding engineer", "devrel"
                            ])
                            candidates.append(LeadershipCandidate(
                                name=aname,
                                role=role,
                                associated_company=target_domain,
                                source_url=page_url,
                                surrounding_text=f"Official JSON-LD BlogPosting author: {aname} ({role})",
                                evidence_type=EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT if has_author_exec else EvidenceCategory.FIRST_PARTY_AUTHOR_BIO,
                                employment_evidence=f"Identified as official author on {target_domain}",
                                verification_status="verified" if has_author_exec else "ambiguous",
                                page_type=page_type,
                                positive_signals=[f"Official first-party author: {aname}"],
                            ))
        except Exception:
            pass

    # 2. Next.js script author metadata
    for s in soup.find_all("script"):
        if s.string and ("author_title" in s.string or "author_url" in s.string):
            for m in re.finditer(r'\{[^{}]*\"author\"\s*:\s*\"([a-z0-9_\-]+)\"[^{}]*\}', s.string):
                obj_str = m.group(0)
                author_slug = m.group(1)
                name_words = [w.capitalize() for w in author_slug.split("_") if w]
                if len(name_words) in [2, 3]:
                    aname = " ".join(name_words)
                    if not is_plausible_person_name(aname, target_org_norm):
                        continue
                    cand_key = f"{aname.lower().strip()}::{page_url}"
                    if cand_key not in seen_candidate_keys:
                        seen_candidate_keys.add(cand_key)
                        title_m = re.search(r'\"author_title\"\s*:\s*\"([^\"]+)\"', obj_str)
                        author_role = title_m.group(1).strip() if title_m else "Author"
                        has_author_exec = any(r in author_role.lower() for r in [
                            "ceo", "cto", "cpo", "coo", "cro", "cfo", "founder", "co-founder",
                            "cofounder", "president", "vp", "chief", "head of", "director",
                            "architect", "lead", "principal", "founding engineer", "devrel"
                        ])
                        assoc_company = None
                        if f'"{target_org_norm}"' in obj_str.lower() or f'"{target_domain}"' in obj_str.lower():
                            assoc_company = target_domain
                        candidates.append(LeadershipCandidate(
                            name=aname,
                            role=author_role,
                            associated_company=assoc_company or target_domain,
                            source_url=page_url,
                            surrounding_text=f"Next.js author metadata: {author_slug} ({author_role})",
                            evidence_type=EvidenceCategory.FIRST_PARTY_EMPLOYEE if has_author_exec else EvidenceCategory.FIRST_PARTY_AUTHOR_BIO,
                            employment_evidence=f"Identified as author/team member on {target_domain}",
                            verification_status="verified" if has_author_exec else "ambiguous",
                            page_type=page_type,
                            positive_signals=[f"Author/team metadata for {target_domain}: {author_role}"],
                        ))

    # 3. DOM Containers
    containers = soup.find_all(
        ["div", "li", "article", "section", "tr", "figcaption", "p", "blockquote"],
        class_=lambda c: c and any(k in str(c).lower() for k in ["team", "people", "member", "card", "founder", "leader", "executive", "bio", "author", "quote", "testimonial"])
    )
    if not containers:
        containers = soup.find_all(["div", "section", "article", "blockquote", "p", "li", "tr"])

    for container in containers:
        text = container.get_text(" ", strip=True)
        if len(text) < 8 or len(text) > 500:
            continue

        text_lower = text.lower()
        if any(nav in text_lower for nav in [
            "on this page", "table of contents", "in this article", "jump to",
            "copy as markdown", "ask chatgpt", "ask claude", "share this"
        ]):
            continue

        candidate_name, candidate_role, associated_company = parse_candidate_identity_and_employer(
            container, text, target_domain
        )
        if not candidate_name:
            continue

        if "(" in candidate_role:
            candidate_role = candidate_role.split("(")[0].strip()
        candidate_role = candidate_role.rstrip(" (,").strip()

        cand_key = f"{candidate_name.lower()}::{page_url}"
        if cand_key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(cand_key)

        lk_url = None
        for a in container.find_all("a", href=True):
            if "linkedin.com/in/" in a["href"].lower() or "linkedin.com/pub/" in a["href"].lower():
                lk_url = a["href"].split("?")[0].rstrip("/")
                break
        if not lk_url:
            lk_url = linkedin_map.get(candidate_name.lower())

        heading = get_nearest_heading(container)
        org_mentions = [
            o for o in extract_org_from_text(text)
            if normalize_org_name(o) != normalize_org_name(candidate_name)
        ]
        if associated_company and associated_company not in org_mentions:
            org_mentions.append(associated_company)

        positive_signals = []
        negative_signals = []
        evidence_type = EvidenceCategory.AMBIGUOUS
        verification_status = "unverified"

        # Check investor context (Tier 4 exclusion)
        is_investor = (
            any(k in heading.lower() for k in ["investor", "investors", "individual investors", "angel", "advisors", "backed"])
            or any(k in text.lower() for k in ["investor", "individual investors", "our investors"])
            or any(k in safe_get_attr(container, "class").lower() for k in ["investor"])
        )
        if is_investor:
            negative_signals.append(f"Located in investor section/heading: '{heading}'")
            evidence_type = EvidenceCategory.INVESTOR
            verification_status = "rejected"

        # Check former/departed employee context
        is_former = (
            any(k in candidate_role.lower() for k in ["former", "ex-", "past", "previously"])
            or any(k in text.lower() for k in ["former ceo", "former cto", "former founder", "ex-ceo", "ex-cto", "previously served as", "stepped down", "left the company"])
        )
        if is_former:
            negative_signals.append("Identified as former or departed employee")
            evidence_type = EvidenceCategory.EXTERNAL_COMPANY_REFERENCE
            verification_status = "rejected"

        # Check external organization association (Tier 4 exclusion)
        if associated_company:
            norm_assoc = normalize_org_name(associated_company)
            if norm_assoc and norm_assoc != target_org_norm:
                negative_signals.append(f"Associated with external organization: '{associated_company}'")
                evidence_type = EvidenceCategory.EXTERNAL_COMPANY_REFERENCE
                verification_status = "rejected"
            elif norm_assoc and norm_assoc == target_org_norm:
                positive_signals.append(f"Associated with target company: '{associated_company}'")

        for org in org_mentions:
            norm_o = normalize_org_name(org)
            if norm_o and norm_o != target_org_norm and norm_o != normalize_org_name(candidate_name):
                if not any(norm_o in sig for sig in negative_signals):
                    negative_signals.append(f"Associated with external organization: '{org}'")
                    if evidence_type == EvidenceCategory.AMBIGUOUS:
                        evidence_type = EvidenceCategory.EXTERNAL_COMPANY_REFERENCE
                        verification_status = "rejected"

        # Check customer/testimonial/quote context (Tier 4 exclusion)
        combined_parent_text = ""
        parent_el = container
        for _ in range(5):
            if parent_el and parent_el.name not in ["body", "html", "[document]"]:
                combined_parent_text += " " + parent_el.get_text(" ", strip=True)
                parent_el = parent_el.parent
        combined_lower = combined_parent_text.lower()

        has_quote_marks = any(q in combined_parent_text for q in ['"', '“', '”'])
        has_target_as_vendor = any(k in combined_lower for k in [
            f"through {target_org_norm}", f"built on {target_org_norm}", f"built with {target_org_norm}",
            f"using {target_org_norm}", f"powered by {target_org_norm}", f"chose {target_org_norm}",
            f"switched to {target_org_norm}", f"migrated to {target_org_norm}", f"runs through {target_org_norm}",
            f"{target_org_norm} allows us", f"{target_org_norm} helped us", f"{target_org_norm} has been",
            "zero to production", "csat", "inbound volume", "our customers call", "our customers"
        ])
        is_quote_card = has_quote_marks and (has_target_as_vendor or any(q in combined_lower for q in ["customer", "testimonial", "case study", "csat", "roi"]))

        is_testimonial = (
            container.name in ["blockquote"]
            or is_quote_card
            or "testimonial" in safe_get_attr(container, "class").lower()
            or "quote" in safe_get_attr(container, "class").lower()
            or any(k in heading.lower() for k in ["customer", "testimonial", "case study", "success story", "partner", "quote", "review", "speaker", "client"])
            or any(k in text.lower() for k in ["testimonial", "customer story", "success story"])
        )
        if is_testimonial:
            negative_signals.append(f"Located in customer/testimonial/partner context under heading '{heading}' or customer quote card")
            if evidence_type not in [EvidenceCategory.INVESTOR, EvidenceCategory.EXTERNAL_COMPANY_REFERENCE]:
                evidence_type = EvidenceCategory.CUSTOMER_STORY
                verification_status = "rejected"

        # Check initiative / report / program / diversity context (Tier 4 exclusion)
        is_initiative_context = (
            any(k in heading.lower() for k in [
                "impact", "initiative", "diversity", "inclusion", "sustainability",
                "annual report", "social impact", "community grants", "climate", "education"
            ])
            or any(k in safe_get_attr(container, "class").lower() for k in ["initiative", "program", "report", "diversity", "impact"])
        )
        has_exec_in_role = any(r in candidate_role.lower() for r in [
            "ceo", "cto", "coo", "cfo", "cro", "cpo", "founder", "co-founder", "cofounder",
            "president", "vp", "chief", "head of"
        ])
        if is_initiative_context and not has_exec_in_role:
            negative_signals.append(f"Located in initiative/program context under heading '{heading}'")
            if evidence_type not in [EvidenceCategory.INVESTOR, EvidenceCategory.EXTERNAL_COMPANY_REFERENCE, EvidenceCategory.CUSTOMER_STORY]:
                evidence_type = EvidenceCategory.AMBIGUOUS
                verification_status = "rejected"

        # Tier 1 & Tier 2 Evaluation (when no negative signals)
        if not negative_signals:
            has_exec_role = any(r in candidate_role.lower() for r in [
                "ceo", "cto", "coo", "cfo", "cro", "cpo", "founder", "co-founder", "cofounder",
                "president", "vp", "chief", "head of"
            ])
            has_team_role = any(r in candidate_role.lower() for r in [
                "architect", "director", "lead", "devrel", "principal", "founding engineer"
            ])

            text_lower = text.lower()
            heading_lower = heading.lower()
            explicit_target_leader = (
                f"our {candidate_role.lower()}" in text_lower
                or (target_org_norm and f"{target_org_norm}'s {candidate_role.lower()}" in text_lower)
                or (target_org_norm and f"{candidate_role.lower()} of {target_org_norm}" in text_lower)
                or (target_org_norm and f"founded {target_org_norm}" in text_lower)
                or (target_org_norm and f"co-founder and ceo" in text_lower)
                or (target_org_norm and f"ceo and co-founder" in text_lower)
                or (target_org_norm and f"co-founder and cto" in text_lower)
                or (target_org_norm and f"cto and co-founder" in text_lower)
                or (target_org_norm and f"{candidate_name.lower()} of {target_org_norm}" in text_lower)
            )
            is_team_heading = any(k in heading_lower for k in [
                "leadership", "the founders", "our team", "leadership team", "executives", "management"
            ])

            if page_type in ["team", "about"] and (is_team_heading or has_exec_role or has_team_role):
                evidence_type = EvidenceCategory.FIRST_PARTY_LEADERSHIP
                verification_status = "verified"
                positive_signals.append(f"Explicit official leadership/team card: {candidate_role}")
            elif explicit_target_leader:
                evidence_type = EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT
                verification_status = "verified"
                positive_signals.append(f"Explicit target company leadership: {candidate_role}")
            elif page_type in ["press", "blog"] and any(k in text_lower for k in ["announced", "appoints", "named", "joined as", "welcomes", "series", "funding", "raises"]):
                evidence_type = EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT
                verification_status = "verified"
                positive_signals.append(f"Official company announcement: {candidate_role}")
            elif has_team_role or (associated_company and normalize_org_name(associated_company) == target_org_norm):
                evidence_type = EvidenceCategory.FIRST_PARTY_EMPLOYEE
                verification_status = "verified"
                positive_signals.append(f"Target company employee/team evidence: {candidate_role}")
            else:
                evidence_type = EvidenceCategory.AMBIGUOUS
                verification_status = "ambiguous"
                positive_signals.append(f"Ambiguous company mention: {candidate_role}")

        candidates.append(LeadershipCandidate(
            name=candidate_name,
            role=candidate_role,
            associated_company=associated_company,
            source_url=page_url,
            surrounding_text=text[:300],
            evidence_type=evidence_type,
            employment_evidence="; ".join(positive_signals if positive_signals else negative_signals),
            linkedin_url=lk_url,
            verification_status=verification_status,
            page_type=page_type,
            section_heading=heading,
            organization_mentions=org_mentions,
            positive_signals=positive_signals,
            negative_signals=negative_signals,
        ))

    return candidates


def compute_evidence_score(candidate: LeadershipCandidate, target_domain: str) -> tuple[float, list[str]]:
    """Compute explicit numerical evidence score according to user decision hierarchy:
    +1.0 explicit official leadership/team card
    +0.9 explicit 'CEO of TARGET' / 'TARGET CEO'
    +0.85 official employee bio
    +0.8 official company announcement
    +0.7 official author profile with leadership/team role
    +0.4 ambiguous company blog mention
    -0.8 customer/testimonial context
    -0.8 partner context
    -1.0 explicitly associated with another company
    -1.0 external speaker/customer/investor context
    """
    score = 0.0
    reasons = []
    target_norm = normalize_org_name(target_domain)

    is_exec = any(r in candidate.role.lower() for r in [
        "ceo", "cto", "coo", "cfo", "cro", "cpo", "founder", "co-founder", "cofounder", "president"
    ])
    is_team_lead = any(r in candidate.role.lower() for r in [
        "architect", "director", "lead", "devrel", "principal", "founding engineer", "vp", "chief", "head of"
    ])

    # 1. Base structural score
    if candidate.evidence_type == EvidenceCategory.FIRST_PARTY_LEADERSHIP or (
        candidate.page_type in ["team", "about"] and any(k in candidate.section_heading.lower() for k in ["team", "leadership", "founder", "people", "executive"])
    ):
        if is_exec:
            score += 1.0
            reasons.append("+1.0 explicit official leadership/team card")
        else:
            score += 0.85
            reasons.append("+0.85 official employee bio")
    elif candidate.evidence_type == EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT:
        score += 0.8
        reasons.append("+0.8 official company announcement")
    elif candidate.evidence_type in [EvidenceCategory.FIRST_PARTY_AUTHOR_BIO, EvidenceCategory.FIRST_PARTY_EMPLOYEE]:
        if is_exec:
            score += 0.8
            reasons.append("+0.8 official company author with executive role")
        elif is_team_lead:
            score += 0.7
            reasons.append("+0.7 official author/employee profile with leadership role")
        else:
            score += 0.4
            reasons.append("+0.4 ambiguous company blog mention")
    elif candidate.page_type in ["team", "about"] and (is_exec or is_team_lead):
        score += 0.7
        reasons.append("+0.7 team/about page candidate with leadership role")
    else:
        score += 0.4
        reasons.append("+0.4 ambiguous candidate mention")

    # 2. Target company possessive / explicit role boost
    text_lower = candidate.surrounding_text.lower()
    role_lower = candidate.role.lower()
    if target_norm and (
        f"{target_norm}'s {role_lower}" in text_lower
        or f"{role_lower} of {target_norm}" in text_lower
        or f"{target_norm} {role_lower}" in text_lower
        or f"founded {target_norm}" in text_lower
    ):
        score = max(score, 0.9)
        reasons.append(f"+0.9 explicit role for target company ({target_norm})")

    # 3. Penalties & negative context
    if candidate.associated_company:
        assoc_norm = normalize_org_name(candidate.associated_company)
        if assoc_norm and assoc_norm != target_norm and assoc_norm != normalize_org_name(candidate.name):
            score -= 1.0
            reasons.append(f"-1.0 explicitly associated with another company ({candidate.associated_company})")

    if candidate.evidence_type == EvidenceCategory.INVESTOR or any("investor" in sig.lower() for sig in candidate.negative_signals):
        score -= 1.0
        reasons.append("-1.0 investor context")

    if candidate.evidence_type in [EvidenceCategory.CUSTOMER_STORY, EvidenceCategory.TESTIMONIAL] or any("customer/testimonial" in sig.lower() for sig in candidate.negative_signals):
        score -= 0.8
        reasons.append("-0.8 customer/testimonial context")

    if candidate.evidence_type == EvidenceCategory.PARTNER_PAGE or any("partner" in sig.lower() for sig in candidate.negative_signals):
        score -= 0.8
        reasons.append("-0.8 partner context")

    if any("former" in sig.lower() or "departed" in sig.lower() for sig in candidate.negative_signals):
        score -= 1.0
        reasons.append("-1.0 former/departed employee context")

    return score, reasons


def evaluate_candidate_affiliation(
    candidate: LeadershipCandidate,
    target_domain: str
) -> CandidateAffiliation:
    """Stage B: Determine whether candidate actually works for target_domain using evidence score."""
    target_org = normalize_org_name(target_domain)

    cleaned_role = candidate.role
    for phrase in ["trusted at", "unified platform", "source:", "(source:"]:
        if phrase in cleaned_role.lower():
            cleaned_role = re.split(re.escape(phrase), cleaned_role, flags=re.IGNORECASE)[0].strip()
    if re.search(r"\b(?:at|of|for)\s+[A-Za-z0-9]+", cleaned_role, re.IGNORECASE):
        cleaned_role = re.split(r"\b(?:at|of|for)\s+[A-Za-z0-9]+", cleaned_role, flags=re.IGNORECASE)[0].strip()

    evidence_score, reasons = compute_evidence_score(candidate, target_domain)
    candidate.evidence_score = evidence_score

    # Threshold 1: score >= 0.70 -> VERIFIED (Tier 1 & Tier 2) -> INCLUDE directly
    if evidence_score >= 0.70:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=candidate.associated_company or target_domain,
            cleaned_role=cleaned_role,
            affiliation="TARGET_COMPANY",
            confidence=min(1.0, max(0.70, round(evidence_score, 2))),
            reason="; ".join(reasons),
            source_url=candidate.source_url,
            linkedin_url=candidate.linkedin_url,
            verification_status="verified",
            evidence_score=round(evidence_score, 2),
        )
    # Threshold 2: 0.40 <= score < 0.70 -> AMBIGUOUS (Tier 3) -> LinkedIn verification required
    elif evidence_score >= 0.40:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=candidate.associated_company,
            cleaned_role=cleaned_role,
            affiliation="UNKNOWN",
            confidence=round(evidence_score, 2),
            reason="; ".join(reasons) + " (ambiguous, requires LinkedIn verification)",
            source_url=candidate.source_url,
            linkedin_url=candidate.linkedin_url,
            verification_status="ambiguous",
            evidence_score=round(evidence_score, 2),
        )
    # Threshold 3: score < 0.40 -> REJECTED (Tier 4 & Tier 5) -> EXCLUDE
    else:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=candidate.associated_company,
            cleaned_role=cleaned_role,
            affiliation="OTHER_COMPANY",
            confidence=max(0.0, round(evidence_score, 2)),
            reason="; ".join(reasons) + " (excluded by evidence score)",
            source_url=candidate.source_url,
            linkedin_url=candidate.linkedin_url,
            verification_status="rejected",
            evidence_score=round(evidence_score, 2),
        )



def preprocess_domain(
    pages: dict[str, str],
    domain: str,
    linkedin_links: list[dict] | None = None,
    crawl_stats: CrawlStats | None = None,
) -> tuple[str, list[str]]:
    """Process all pages for a domain into a single context string with evidence tracking.

    Args:
        pages: Dictionary of URL -> HTML content
        domain: The domain being processed (for logging)
        linkedin_links: Direct LinkedIn links discovered during crawling
        crawl_stats: Optional CrawlStats object to populate with candidate metrics

    Returns:
        Tuple of (aggregated_markdown, all_emails)
    """
    logger.info(f"🔧 Preprocessing {len(pages)} pages for {domain}")

    all_sections: list[str] = []
    all_emails: list[str] = []
    total_chars = 0
    max_total_chars = MAX_TOKENS_TOTAL * CHARS_PER_TOKEN

    sorted_pages = sorted(pages.items(), key=lambda item: 0 if classify_page_type(item[0]) in ["team", "about"] else 1)

    for url, html in sorted_pages:
        if total_chars >= max_total_chars:
            logger.info("  ⚠ Token budget reached, skipping remaining pages")
            break

        if not html or not isinstance(html, str) or not html.strip():
            logger.warning(f"  ⚠ Skipping empty/invalid page content for {url}")
            continue

        try:
            page_markdown, page_emails = preprocess_page(url, html)
            all_emails.extend(page_emails)

            if page_markdown:
                section = f"PAGE URL: {url}\n\nCONTENT:\n{page_markdown}"
                remaining = max_total_chars - total_chars
                if len(section) > remaining:
                    section = truncate_text(section, remaining)

                all_sections.append(section)
                total_chars += len(section)
        except Exception as exc:
            logger.warning(f"  ⚠ Error preprocessing page {url}, skipping: {exc}")

    unique_emails = list(dict.fromkeys(all_emails))

    # Stage A Candidate Discovery & Evidence Context Gathering
    all_candidates: list[LeadershipCandidate] = []
    for url, html in pages.items():
        if not html or not isinstance(html, str) or not html.strip():
            continue
        try:
            all_candidates.extend(extract_candidate_evidence_from_page(url, html, domain, linkedin_links))
        except Exception as exc:
            logger.warning(f"  ⚠ Error extracting candidates from {url}, skipping: {exc}")

    # Deduplicate candidates across pages by canonical name (first + last)
    unique_candidates_dict: dict[str, LeadershipCandidate] = {}
    for cand in all_candidates:
        c_parts = cand.name.split()
        if len(c_parts) < 2:
            continue
        c_key = f"{c_parts[0].lower()} {c_parts[-1].lower()}"

        if c_key not in unique_candidates_dict:
            unique_candidates_dict[c_key] = cand
        else:
            existing = unique_candidates_dict[c_key]
            # Prioritize verified over unverified
            if cand.verification_status == "verified" and existing.verification_status != "verified":
                unique_candidates_dict[c_key] = cand
            elif cand.verification_status == existing.verification_status:
                # Prioritize executive roles over Key Team Member
                cand_has_exec = any(r in cand.role.lower() for r in ["ceo", "cto", "cpo", "founder", "president", "vp", "architect", "lead", "director"])
                exist_has_exec = any(r in existing.role.lower() for r in ["ceo", "cto", "cpo", "founder", "president", "vp", "architect", "lead", "director"])
                if cand_has_exec and not exist_has_exec:
                    unique_candidates_dict[c_key] = cand

    unique_candidates = list(unique_candidates_dict.values())
    metrics.candidates_found += len(unique_candidates)

    # Stage B Affiliation Evaluation
    affiliations: list[CandidateAffiliation] = [
        evaluate_candidate_affiliation(cand, domain) for cand in unique_candidates
    ]

    verified_affiliations = [a for a in affiliations if a.affiliation == "TARGET_COMPANY" and a.confidence >= 0.7]
    excluded_affiliations = [a for a in affiliations if a.affiliation != "TARGET_COMPANY" or a.confidence < 0.7]

    metrics.candidates_verified += len(verified_affiliations)
    metrics.candidates_rejected += len(excluded_affiliations)

    if crawl_stats is not None:
        crawl_stats.candidates_detected = len(unique_candidates)
        crawl_stats.candidates_verified = len(verified_affiliations)
        crawl_stats.candidates_rejected = len(excluded_affiliations)

    evidence_lines: list[str] = [f"=== TARGET COMPANY: {domain} ==="]
    if unique_emails:
        evidence_lines.append("Extracted Emails:")
        evidence_lines.extend(f"- {e}" for e in unique_emails)

    if verified_affiliations:
        evidence_lines.append(f"TARGET COMPANY LEADERSHIP EVIDENCE (Verified Employees/Founders of {domain}):")
        for a in verified_affiliations:
            lk_info = f" [LinkedIn: {a.linkedin_url}]" if a.linkedin_url else ""
            evidence_lines.append(f"- Name: {a.name} | Role: {a.cleaned_role}{lk_info} (Source: {a.source_url})")

    if excluded_affiliations:
        evidence_lines.append("EXCLUDED INDIVIDUALS (CUSTOMERS / PARTNERS / INVESTORS / TESTIMONIALS - EXCLUDED FROM LEADERSHIP):")
        for ex in excluded_affiliations:
            assoc = f" | Associated Company: {ex.associated_company}" if ex.associated_company else ""
            evidence_lines.append(f"- Excluded: {ex.name} ({ex.role}){assoc} | Reason: {ex.reason} | Source: {ex.source_url}")

    evidence_lines.append("================================\n")

    evidence_header = "\n".join(evidence_lines) + "\n\n"
    aggregated = evidence_header + "\n\n---\n\n".join(all_sections)

    logger.info(
        f"  ✅ Preprocessing complete: "
        f"{len(aggregated):,} chars (~{len(aggregated) // CHARS_PER_TOKEN:,} tokens), "
        f"{len(unique_emails)} emails found, {len(verified_affiliations)} verified leadership candidates, "
        f"{len(excluded_affiliations)} excluded"
    )

    return aggregated, unique_emails


