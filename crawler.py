"""
crawler.py — Smart Playwright-based Web Crawler with Recursive Link Discovery & Scoring

Handles headless browser automation to fetch JavaScript-rendered pages.
Discovers and ranks relevant subpages (about, team, contact, pricing, solutions, etc.)
using dynamic link analysis and recursive depth crawling.
"""

import asyncio
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from config import (
    BROWSER_TIMEOUT_MS,
    PAGE_WAIT_MS,
    MAX_SUBPAGES,
    CRAWL_DELAY_S,
)
from typing import NamedTuple, Optional
from models import CrawlStats, SelectedPageInfo
from utils import (
    logger,
    domain_to_url,
    is_same_domain,
    resolve_url,
    normalize_domain,
    extract_emails_from_text,
)


class CandidateLink(NamedTuple):
    """Internal representation of a discovered link with semantic relevance metadata."""

    url: str
    score: int
    reason: str = ""
    is_leadership_relevant: bool = False
    anchor_text: str = ""


# Generalized semantic keyword sets for multi-factor link discovery
LEADERSHIP_CORE_SIGNALS = {
    "leadership", "leaders", "executive", "executives", "management",
    "officer", "officers", "board", "directors", "director", "governance",
    "csuite", "c-suite", "ceo", "cto", "cpo", "coo", "cro", "cfo", "president",
    "vice-president", "vice president", "vp"
}

FOUNDER_CORE_SIGNALS = {
    "founder", "founders", "founding", "cofounder", "co-founder",
    "our-story", "founding-story", "how-we-started", "the-founders",
    "letter-from-our", "message-from-the-ceo", "from-our-ceo"
}

PEOPLE_TEAM_SIGNALS = {
    "team", "our-team", "people", "staff", "employees", "who-we-are",
    "meet-the-team", "meet-our", "meet-us", "profiles", "bio", "biography",
    "biographies", "authors", "byline", "contributors"
}

COMPANY_ABOUT_SIGNALS = {
    "about", "about-us", "company", "our-mission", "mission", "overview",
    "organization", "who-we-", "how-we-"
}

ANNOUNCEMENT_NEWS_SIGNALS = {
    "press", "news", "announcements", "press-release", "media", "announcement",
    "series-", "funding", "seed", "raises", "milestone", "newsroom", "insights"
}

CAREER_CULTURE_SIGNALS = {
    "careers", "jobs", "culture", "working-at", "life-at"
}

CONTACT_SIGNALS = {
    "contact", "contact-us", "contact-sales", "sales", "get-in-touch", "reach-us", "support"
}

PRICING_SIGNALS = {
    "pricing", "plans", "pricing-plans", "tier"
}

PENALTY_KEYWORDS = {
    "login", "signup", "auth", "register", "cart", "checkout", "app", "dashboard",
    "privacy", "terms", "cookie-policy", "legal", "gdpr", "status", "rss"
}

STATIC_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip", ".css", ".js",
    ".ico", ".webp", ".xml", ".txt", ".mp4", ".woff", ".ttf"
}


# Short executive acronyms that require word-boundary matching to avoid
# substring false positives (e.g., "cto" inside "vector", "cro" inside "cron")
_SHORT_KEYWORDS = {w for w in LEADERSHIP_CORE_SIGNALS if len(w) <= 3}

# All positive people/leadership signals, combined for compound-path analysis
_ALL_PEOPLE_SIGNALS = LEADERSHIP_CORE_SIGNALS | FOUNDER_CORE_SIGNALS | PEOPLE_TEAM_SIGNALS

# Corporate topical namespaces that must NOT be treated as dedicated leadership pages
NON_LEADERSHIP_CORPORATE_TOPICS = {
    "diversity", "inclusion", "belonging", "culture", "life-at", "careers", "jobs",
    "social-impact", "community", "communities", "initiatives", "programs",
    "sustainability", "esg", "csr", "policy", "policies", "guidelines", "terms",
    "privacy", "legal", "compliance", "security", "developer-policy", "brand", "press-kit",
}

# Dedicated leadership/team/people indicators — pages whose primary topic IS personnel
_DEDICATED_PEOPLE_KEYWORDS = {
    "leadership", "leaders", "executive", "executives", "governance",
    "csuite", "c-suite", "board", "directors",
    "founder", "founders", "cofounder", "co-founder", "the-founders",
    "our-leadership", "management-team", "executive-team", "leadership team"
}


def _wb_match(keyword: str, text: str) -> bool:
    """Word-boundary match: returns True if keyword appears as a whole word in text.

    Prevents substring false positives like 'cto' matching inside 'vector'.
    """
    if not text or not keyword:
        return False
    # For multi-word keywords (e.g., "vice president"), use simple substring
    if " " in keyword:
        return keyword in text
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))


CORPORATE_INFO_ROOTS = {
    "about", "about-us", "company", "our-story", "who-we-are", "corporate", "organization"
}


def _is_standalone_leadership_in_path(keyword: str, path: str) -> bool:
    """Check if a leadership keyword in a URL path indicates the page is ABOUT corporate leadership.

    Reasons about path structure:
    1. Single segment: e.g. /leadership, /our-leadership, /management-team
       -> True if standalone or leadership compound.
    2. Multi-segment under corporate root: e.g. /about/leadership, /company/team, /who-we-are/leadership
       -> True if parent segment is a recognized corporate informational root.
    3. Multi-segment under dedicated personnel root: e.g. /team/cto, /governance/advisors
       -> True if first segment is team/governance and second segment is an executive role or advisory directory.
    4. Arbitrary multi-segment paths (e.g. /Leadership/Fb-sript, /workspace/team/settings) or
       non-leadership topics (diversity, culture, policy, etc.):
       -> False. A platform tenant/project/action path or corporate culture/policy path
          does NOT become corporate leadership content merely because a segment happens to equal a leadership word.
    """
    segments = [s.lower() for s in path.split("/") if s]
    if not segments:
        return False

    # Never treat corporate diversity, culture, policy, etc. pages as corporate leadership
    if any(s in NON_LEADERSHIP_CORPORATE_TOPICS for s in segments):
        return False

    matching_indices = [i for i, seg in enumerate(segments) if _wb_match(keyword, seg)]
    if not matching_indices:
        return False

    # 1. Single-segment path: /leadership, /management-team, /our-leadership
    if len(segments) == 1:
        seg = segments[0]
        if seg == keyword:
            return True
        parts = [p for p in re.split(r'[-_]', seg) if p]
        if keyword in parts:
            other_parts = [p for p in parts if p != keyword]
            _ALLOWED_MODIFIERS = {"our", "the", "meet", "key", "senior", "new", "all", "and"}
            if all(p in _ALL_PEOPLE_SIGNALS or p in COMPANY_ABOUT_SIGNALS or p in _ALLOWED_MODIFIERS for p in other_parts):
                return True
        return False

    # 2. Multi-segment under corporate informational root: /about/leadership, /company/team
    root = segments[0]
    if root in CORPORATE_INFO_ROOTS:
        for idx in matching_indices:
            if idx > 0:
                seg = segments[idx]
                if seg == keyword:
                    return True
                parts = [p for p in re.split(r'[-_]', seg) if p]
                if keyword in parts:
                    other_parts = [p for p in parts if p != keyword]
                    _ALLOWED_MODIFIERS = {"our", "the", "meet", "key", "senior", "new", "all", "and"}
                    if all(p in _ALL_PEOPLE_SIGNALS or p in COMPANY_ABOUT_SIGNALS or p in _ALLOWED_MODIFIERS for p in other_parts):
                        return True
        return False

    # 3. Dedicated role/governance subpath: /team/cto, /governance/advisors
    if root in {"governance", "team", "people"} and len(segments) == 2:
        sub = segments[1]
        if sub in {"cto", "ceo", "cfo", "coo", "cpo", "cro", "advisors", "directors", "board", "executives"}:
            return True

    # 4. Arbitrary multi-segment platform paths (e.g. /Leadership/Fb-sript) MUST NOT inherit leadership relevance
    return False


# Locale-prefix regex: matches /xx/ or /xx-yy/ at the start of a URL path
_LOCALE_PREFIX_RE = re.compile(
    r'^/(?:[a-z]{2}|[a-z]{2}-[a-z]{2})(/.*)', re.IGNORECASE
)

# ─── Platform / UGC / Tenant Action Indicators ──────────────────────────────
# Common action segments indicating developer, repository, tenant, or application
# workflows on multi-tenant platforms (GitHub, GitLab, Docker Hub, SaaS tools, etc.)
PLATFORM_ACTION_SEGMENTS = {
    "commits", "commit", "graphs", "contributors", "blob", "tree", "branches",
    "tags", "pulls", "pull", "issues", "releases", "packages", "forks",
    "watchers", "stars", "network", "compare", "archive", "raw", "blame",
    "deployments", "console", "workspaces", "workspace", "billing",
    "api-keys", "tokens", "project-settings", "settings", "environments",
    "actions", "security", "pulse", "discussions", "wiki", "milestones",
}

# Recognized corporate root path segments (first segment after locale prefix)
CORPORATE_ROOT_STEMS = {
    "about", "about-us", "company", "our-story", "who-we-are", "team", "our-team",
    "people", "leadership", "executives", "management", "board", "governance",
    "directors", "careers", "jobs", "culture", "life-at", "press", "news", "newsroom",
    "blog", "stories", "media", "announcements", "contact", "contact-us", "support",
    "legal", "privacy", "terms", "trust", "compliance", "social-impact",
    "diversity", "inclusion", "sustainability", "esg", "investors", "investor-relations",
    "ir", "pricing", "solutions", "enterprise", "customers", "partners", "overview",
}

# Recognized corporate article/news root stems
ARTICLE_ROOT_STEMS = {
    "blog", "news", "newsroom", "press", "announcements", "media", "stories", "articles", "insights"
}

# Subpaths representing individual customer/partner case studies or client profiles
CUSTOMER_CASE_STUDY_SUBPATHS = (
    "/customers/", "/customer/", "/customer-stories/", "/customer-story/",
    "/case-studies/", "/case-study/", "/clients/", "/client-stories/",
    "/partners/", "/partner-stories/",
)

# Dedicated personnel indicator segments
DEDICATED_PERSONNEL_STEMS = {
    "leadership", "team", "our-team", "people", "executives", "board",
    "directors", "governance", "management-team", "our-leadership", "founders",
    "meet-the-team", "meet-our-team", "who-we-are", "our-people"
}

# Product-plan keywords that distinguish pricing/plans from personnel directories
_PRODUCT_PLAN_KEYWORDS = {
    "plans", "pricing", "tier", "features", "subscriptions", "small and medium teams",
    "team plans", "teams plan", "starter", "pro", "enterprise plan", "for teams"
}


def _canonical_path(url: str) -> str:
    """Produce a canonical URL path by stripping locale prefixes.

    Allows deduplication of equivalent pages served under different locale
    prefixes (e.g., /in/about and /about, /en-us/team and /team).
    """
    path = urlparse(url).path.rstrip("/") or "/"
    m = _LOCALE_PREFIX_RE.match(path)
    if m:
        return m.group(1).rstrip("/") or "/"
    return path


def _deduplicate_candidates(
    candidates: dict[str, "CandidateLink"],
) -> dict[str, "CandidateLink"]:
    """Remove duplicate candidates that map to the same canonical path.

    When two URLs differ only by locale prefix (e.g., /about vs /in/about),
    keeps the one with the higher score.  Genuinely different pages (different
    canonical paths) are never merged.
    """
    canonical_map: dict[str, list[str]] = {}  # canonical_path -> [url, ...]
    for url in candidates:
        cp = _canonical_path(url)
        canonical_map.setdefault(cp, []).append(url)

    deduped: dict[str, "CandidateLink"] = {}
    for cp, urls in canonical_map.items():
        # Keep the URL with the highest score (prefer leadership-relevant on tie)
        best_url = max(
            urls,
            key=lambda u: (
                candidates[u].score,
                candidates[u].is_leadership_relevant,
            ),
        )
        deduped[best_url] = candidates[best_url]
    return deduped



def _candidate_sort_key(cand: "CandidateLink") -> tuple:
    """Sort key for candidate selection and crawl frontier prioritization.

    Tiers:
    1. is_leadership_relevant: genuinely leadership/people pages always take top priority.
    2. not is_fallback: discovered pages (real links found on site) beat speculative fallbacks.
    3. score: relevance score as the tiebreaker within each tier.
    """
    is_fallback = cand.reason.startswith("Fallback")
    return (
        cand.is_leadership_relevant,
        not is_fallback,
        cand.score,
    )


PRODUCT_ROOT_STEMS = {
    "product", "products", "features", "solutions", "solution", "integrations",
    "docs", "documentation", "guides", "templates", "catalog", "pricing",
    "enterprise", "customers", "partners", "resources", "security", "overview",
    "services", "platform", "developers", "api", "tools", "apps", "downloads",
}


def _classify_url_origin(url: str) -> str:
    """Classify the origin and intent of a URL generically across arbitrary domains.

    Distinguishes:
      - 'auth': login/registration/authentication routes
      - 'platform_ugc': user-generated or platform/repository/tenant paths
      - 'article': corporate blog/news/press announcements
      - 'product': product marketing/plan/feature pages
      - 'corporate_leadership': dedicated corporate personnel directory
      - 'corporate_general': general corporate informational pages (/about, /company)
    """
    path = _canonical_path(url).strip("/")
    if not path:
        return "homepage"

    segments = [s.lower() for s in path.split("/") if s]
    if not segments:
        return "homepage"

    _AUTH_PATH_SEGMENTS = {"login", "signin", "signup", "join", "register", "auth", "sso", "password", "session"}
    if any(s in _AUTH_PATH_SEGMENTS for s in segments):
        return "auth"

    # Platform action segments (e.g. /commits, /graphs, /blob, /tree)
    if any(s in PLATFORM_ACTION_SEGMENTS for s in segments):
        return "platform_ugc"

    first_seg = segments[0]

    # Article/News roots
    if first_seg in ARTICLE_ROOT_STEMS:
        return "article"

    # Product/Documentation roots
    if first_seg in PRODUCT_ROOT_STEMS:
        return "product"

    # Sub-path under corporate informational roots:
    # e.g., /about/leadership, /company/team, /who-we-are/leadership, /about/our-team
    if first_seg in CORPORATE_INFO_ROOTS:
        if len(segments) >= 2 and any(s in DEDICATED_PERSONNEL_STEMS for s in segments[1:]):
            if not any(s in NON_LEADERSHIP_CORPORATE_TOPICS for s in segments):
                return "corporate_leadership"
        return "corporate_general"

    # Single-segment personnel path: /leadership, /our-team, /executives, /board, /team
    if len(segments) == 1 and first_seg in DEDICATED_PERSONNEL_STEMS:
        return "corporate_leadership"

    # Multi-segment executive role: /team/cto, /governance/advisors
    if len(segments) == 2 and first_seg in {"team", "people", "governance"}:
        if segments[1] in {"cto", "ceo", "coo", "cfo", "cpo", "cro", "advisors", "board", "directors"}:
            return "corporate_leadership"
        return "individual_profile_candidate"

    # If the root is a dedicated personnel stem but has arbitrary multi-segments (e.g. /Leadership/Fb-sript),
    # this is a platform/user/repo or tenant path, not a corporate informational directory
    if first_seg in DEDICATED_PERSONNEL_STEMS and len(segments) >= 2:
        return "platform_ugc"

    if first_seg in CORPORATE_ROOT_STEMS:
        return "corporate_general"

    # Multi-segment path where root is not a known corporate/product/article stem
    # e.g., /user/repo or /tenant/project/... (e.g. /Leadership/Fb-sript)
    if len(segments) >= 2:
        return "platform_ugc"

    return "unknown"
 
 
def _is_article_root(url: str, path: str = "") -> bool:
    """Check if a URL represents an article/news/blog root or listing directory,
    rather than an individual article, product story, or customer profile.

    Roots:
      - /blog, /news, /newsroom, /press, /insights, /stories, /announcements, /articles
      - /company/news, /about/news, /about/press, /resources/blog, /company/blog
      - /blog/page/2, /blog/category/engineering
    Individual articles (not roots):
      - /blog/meet-notions-board-of-directors
      - /news/2024-01-funding-announcement
      - /customer-stories/ramp
    """
    if not path:
        path = _canonical_path(url).strip("/").lower()
    else:
        path = path.strip("/").lower()
    if not path:
        return False

    segments = [s for s in path.split("/") if s]
    if not segments:
        return False

    first_seg = segments[0]

    # Single segment matching an article root stem: e.g. /blog, /news, /press, /stories, /insights
    if len(segments) == 1 and first_seg in ARTICLE_ROOT_STEMS:
        return True

    # Two segments under corporate/resource root: e.g. /company/news, /about/press, /resources/blog
    if len(segments) == 2:
        if first_seg in CORPORATE_INFO_ROOTS or first_seg in {"resources", "media", "content"}:
            if segments[1] in ARTICLE_ROOT_STEMS:
                return True

    # Pagination or category index: e.g. /blog/category/..., /blog/page/..., /blog/tag/...
    if len(segments) == 2 and first_seg in ARTICLE_ROOT_STEMS:
        if segments[1] in {"category", "categories", "tag", "tags", "page", "topics", "all", "archive"}:
            return True

    return False


def _inspect_page_content_identity(html: str, url: str, final_url: str) -> str:
    """Inspect fetched HTML content to classify the resulting page identity.

    Distinguishes:
      - 'auth': login, signin, or SSO auth screen
      - 'platform_ugc': user profile, repository, tenant project workspace
      - 'corporate_leadership': legitimate corporate leadership, executive, or team directory
      - 'article': newsroom, press release, corporate announcement
      - 'product': product/team subscription plan, feature overview
      - 'corporate_general': company informational page
      - 'other': generic page
    """
    if not html or len(html) < 200:
        return "other"

    head_snippet = html[:80000].lower()

    # Extract title with entity unescaping
    title_match = re.search(r"<title[^>]*>(.*?)</title>", head_snippet, re.IGNORECASE | re.DOTALL)
    title_text = ""
    if title_match:
        from html import unescape
        title_text = unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip().lower()

    # 1. Auth screen detection
    auth_title_markers = ("sign in", "log in", "login", "signin", "sign up", "signup", "authentication", "sso")
    if any(m in title_text for m in auth_title_markers):
        return "auth"
    if 'type="password"' in head_snippet or "type='password'" in head_snippet:
        return "auth"

    # 2. Platform / User Profile / Repository detection
    # If the URL is under a corporate informational root (e.g. /about/leadership),
    # it is a first-party corporate page, never a tenant or user profile.
    parsed_path = urlparse(url).path.strip("/")
    url_segs = [s.lower() for s in parsed_path.split("/") if s]
    if url_segs and len(url_segs[0]) == 2:  # locale prefix like /in/ or /en/
        url_segs = url_segs[1:]
    is_corporate_root_path = bool(url_segs and url_segs[0] in CORPORATE_INFO_ROOTS)

    if not is_corporate_root_path:
        if (
            'property="og:type" content="profile"' in head_snippet
            or 'content="profile" property="og:type"' in head_snippet
            or 'property="profile:username"' in head_snippet
            or 'name="route-controller" content="profiles"' in head_snippet
            or 'content="profiles" name="route-controller"' in head_snippet
            or 'data-controller="profile"' in head_snippet
        ):
            return "platform_ugc"

        ugc_title_patterns = (
            r"\([^)]+\)\s*[·|•\-—]\s*",
            r"^[a-z0-9_-]+/[a-z0-9_.-]+\s*[:·|•\-—]",
            r"^[a-z0-9_-]+\s*-\s*overview",
        )
        if any(re.search(pat, title_text) for pat in ugc_title_patterns):
            return "platform_ugc"

        # Platform-specific body indicators
        if any(ind in head_snippet for ind in ("pinned repositories", "popular repositories", "contributions in the last year", "overview · repositories")):
            return "platform_ugc"
        ugc_body_indicators = (
            "pinned repositories", "popular repositories", "contributions in the last year",
            "commit activity", "pull requests", "forks", "stars", "user-profile",
            "data-octolytics-dimension-user_id", 'itemtype="http://schema.org/softwareuser"',
            'itemtype="http://schema.org/softwaresourcecode"'
        )
        if sum(1 for ind in ugc_body_indicators if ind in head_snippet) >= 2:
            return "platform_ugc"

    # 3. Product / Team Plan marketing detection
    product_plan_title_markers = ("for teams", "plans for", "pricing for", "compare plans", "plan & pricing")
    if any(m in title_text for m in product_plan_title_markers):
        return "product"

    # 4. Dedicated leadership indicators
    lead_markers = (
        "executive leadership", "leadership team", "board of directors",
        "executive officers", "our leaders", "our leadership", "executive team", "meet our team"
    )
    if any(m in head_snippet for m in lead_markers):
        return "corporate_leadership"

    return "other"


def evaluate_link_relevance(
    resolved_url: str,
    anchor_text: str = "",
    title_text: str = "",
    surrounding_text: str = "",
    parent_heading: str = "",
    nav_context: str = "",
    rel_attr: str = "",
) -> tuple[int, str, bool, list[str], list[str]]:
    """Score a candidate link using semantic signals with context-aware tiering.

    Distinguishes between pages genuinely ABOUT leadership/people vs. pages that
    merely contain a leadership keyword in a product/feature/technical context.

    Returns:
        tuple[int, str, bool, list[str], list[str]]:
            (score, reason_string, is_leadership_relevant, positive_signals, negative_signals)
    """
    parsed = urlparse(resolved_url)
    path = parsed.path.rstrip("/").lower()
    positive_signals: list[str] = []
    negative_signals: list[str] = []

    # Check static extensions
    if any(path.endswith(ext) for ext in STATIC_EXTENSIONS):
        return -100, "Static asset extension", False, [], ["static file"]

    # Skip empty or homepage root path
    if not path or path == "/":
        return 0, "Homepage root", False, [], []

    # ── Origin Classification Filter ───────────────────────────────────────
    origin = _classify_url_origin(resolved_url)
    if origin == "platform_ugc":
        return -30, "Platform/UGC path (not first-party corporate)", False, [], ["platform-ugc"]
    if origin == "auth":
        return -30, "Authentication/account route", False, [], ["auth-path"]

    # Penalty check (auth, legal, admin, status, etc.)
    combined_penalty = f"{path} {anchor_text.lower()} {title_text.lower()} {nav_context.lower()}"
    matched_penalties = [p for p in PENALTY_KEYWORDS if _wb_match(p, combined_penalty)]
    if matched_penalties:
        negative_signals.extend(f"penalty:{p}" for p in matched_penalties)
        return -15, f"Penalized keywords: {', '.join(matched_penalties)}", False, [], negative_signals

    score = 0
    reasons: list[str] = []
    is_leadership = False

    anchor_lower = f"{anchor_text} {title_text}".lower()
    surrounding_lower = f"{surrounding_text} {parent_heading} {nav_context}".lower()

    # Helper: match keyword against text using word-boundary for short keywords
    def _match_set_wb(signal_set: set[str], text: str) -> list[str]:
        """Match signal keywords against text, using word-boundary for short keywords."""
        matched = []
        for w in signal_set:
            if len(w) <= 3:
                if _wb_match(w, text):
                    matched.append(w)
            else:
                if w in text:
                    matched.append(w)
        return matched

    # ── 1. Founder signals ──────────────────────────────────────────────────
    founders_path = _match_set_wb(FOUNDER_CORE_SIGNALS, path)
    founders_anchor = _match_set_wb(FOUNDER_CORE_SIGNALS, anchor_lower)
    founders_surr = _match_set_wb(FOUNDER_CORE_SIGNALS, surrounding_lower)
    if founders_path or founders_anchor:
        signal = (founders_path or founders_anchor)[0]
        # Verify path match is standalone (not product compound)
        is_standalone_path = not founders_path or _is_standalone_leadership_in_path(signal, path)
        if is_standalone_path:
            score = max(score, 38)
            reasons.append(f"Founder signal in link: '{signal}' (+38)")
            positive_signals.append(f"founder:{signal}")
            is_leadership = True
        else:
            score = max(score, 10)
            reasons.append(f"Founder keyword in compound path: '{signal}' (+10)")
            negative_signals.append(f"founder-in-compound:{signal}")
    elif founders_surr:
        signal = founders_surr[0]
        score = max(score, 20)
        reasons.append(f"Founder signal in surrounding context: '{signal}' (+20)")
        positive_signals.append(f"founder-context:{signal}")
        # Surrounding-only does NOT set is_leadership

    # ── 2. Leadership & Executive signals ───────────────────────────────────
    lead_path = _match_set_wb(LEADERSHIP_CORE_SIGNALS, path)
    lead_anchor = _match_set_wb(LEADERSHIP_CORE_SIGNALS, anchor_lower)
    lead_surr = _match_set_wb(LEADERSHIP_CORE_SIGNALS, surrounding_lower)

    if lead_path:
        # Path contains leadership keyword — check if standalone or product compound
        signal = lead_path[0]
        if _is_standalone_leadership_in_path(signal, path):
            score = max(score, 35)
            reasons.append(f"Leadership page signal in path: '{signal}' (+35)")
            positive_signals.append(f"leadership-path:{signal}")
            is_leadership = True
        else:
            # Product compound (e.g., money-management, bot-management)
            score = max(score, 8)
            reasons.append(f"Leadership keyword in product compound: '{signal}' (+8, not leadership)")
            negative_signals.append(f"product-compound:{signal}")

    if lead_anchor and not is_leadership:
        signal = lead_anchor[0]
        # Anchor text explicitly mentions leadership — check if it's a dedicated indicator
        if signal in _DEDICATED_PEOPLE_KEYWORDS:
            score = max(score, 35)
            reasons.append(f"Leadership/people signal in anchor: '{signal}' (+35)")
            positive_signals.append(f"leadership-anchor:{signal}")
            is_leadership = True
        else:
            # Executive acronym in anchor — moderate signal, not automatically leadership page
            score = max(score, 20)
            reasons.append(f"Executive keyword in anchor: '{signal}' (+20)")
            positive_signals.append(f"exec-anchor:{signal}")

    if lead_surr and not is_leadership:
        signal = lead_surr[0]
        # Surrounding context only — useful score boost but does NOT set leadership relevant
        score = max(score, 12)
        reasons.append(f"Leadership keyword in surrounding context: '{signal}' (+12)")
        positive_signals.append(f"leadership-context:{signal}")

    # ── 3. People & Team signals ────────────────────────────────────────────
    path_segs = [s for s in path.split("/") if s]
    is_non_leadership_topic = any(s in NON_LEADERSHIP_CORPORATE_TOPICS for s in path_segs)
    is_customer_case_study = (
        any(p in path for p in CUSTOMER_CASE_STUDY_SUBPATHS)
        or (len(path_segs) >= 2 and path_segs[0] in {"customers", "customer", "case-studies", "clients", "partners"})
    )

    people_path = _match_set_wb(PEOPLE_TEAM_SIGNALS, path)
    people_anchor = _match_set_wb(PEOPLE_TEAM_SIGNALS, anchor_lower)
    people_surr = _match_set_wb(PEOPLE_TEAM_SIGNALS, surrounding_lower)
    
    # Check if anchor or context indicates product-plan marketing rather than personnel
    is_product_plan = (
        any(_wb_match(k, f"{anchor_lower} {path}") for k in _PRODUCT_PLAN_KEYWORDS)
        or origin == "product"
        or any(k in f"{anchor_lower} {path}" for k in ["for teams", "github team", "team plan", "teams plan"])
    )

    if people_path or people_anchor:
        signal = (people_path or people_anchor)[0]
        # Dedicated personnel paths: /team, /our-team, /meet-the-team, /who-we-are, or nested /about/leadership, /about/team
        is_personnel_path = (
            (len(path_segs) == 1 and path_segs[0] in {"team", "our-team", "people", "meet-the-team", "who-we-are"})
            or (len(path_segs) >= 2 and path_segs[0] in CORPORATE_INFO_ROOTS and any(s in DEDICATED_PERSONNEL_STEMS for s in path_segs[1:]))
        ) and not is_non_leadership_topic
        is_dedicated_anchor = any(
            w in anchor_lower for w in [
                "our team", "meet the team", "meet our team", "who we are",
                "our leadership", "executive leadership", "leadership team",
                "management team", "executive team", "board of directors", "meet the leadership"
            ]
        )
        if (is_personnel_path or is_dedicated_anchor) and not is_product_plan and not is_non_leadership_topic and origin != "unknown":
            score = max(score, 30)
            reasons.append(f"Corporate team signal in link: '{signal}' (+30)")
            positive_signals.append(f"people:{signal}")
            is_leadership = True
        elif is_product_plan:
            score = max(score, 12)
            reasons.append(f"Product/plan signal in link: '{signal}' (+12, not leadership)")
            negative_signals.append(f"product-plan:{signal}")
        else:
            score = max(score, 18)
            reasons.append(f"People/team keyword in link: '{signal}' (+18)")
            positive_signals.append(f"people-weak:{signal}")
    elif people_surr:
        if not (is_product_plan or is_customer_case_study or origin == "product"):
            signal = people_surr[0]
            score = max(score, 10)
            reasons.append(f"People/team keyword in surrounding context: '{signal}' (+10)")
            positive_signals.append(f"people-context:{signal}")

    # Articles, products, and platform routes can NEVER have is_leadership_relevant = True
    if is_non_leadership_topic or origin in ("article", "product", "platform_ugc", "unknown"):
        is_leadership = False

    # ── 4. Company & About signals (NEVER leadership-relevant on their own) ─
    # Differentiate path-level intent (dedicated /about page) from incidental
    # anchor/context mentions ("learn about our product").
    about_path = _match_set_wb(COMPANY_ABOUT_SIGNALS, path)
    about_anchor = _match_set_wb(COMPANY_ABOUT_SIGNALS, anchor_lower)
    about_surr = _match_set_wb(COMPANY_ABOUT_SIGNALS, surrounding_lower)
    if about_path:
        signal = about_path[0]
        score = max(score, 28)
        reasons.append(f"Company/about signal in path: '{signal}' (+28)")
        positive_signals.append(f"about-path:{signal}")
    elif about_anchor:
        signal = about_anchor[0]
        # Anchor-only match: lower score — "learn about..." in anchor text
        # is weaker evidence than a dedicated /about URL path segment
        score = max(score, 15)
        reasons.append(f"Company/about keyword in anchor text: '{signal}' (+15)")
        positive_signals.append(f"about-anchor:{signal}")
    elif about_surr:
        if not (is_product_plan or is_customer_case_study or origin == "product"):
            signal = about_surr[0]
            score = max(score, 8)
            reasons.append(f"Company/about signal in context: '{signal}' (+8)")

    # ── 5. Corporate Article & News Roots (+24) ─────────────────────────────
    # High-value corporate content root that indexes announcements and bylines
    # (e.g. /blog, /news, /newsroom, /press, /insights, /stories)
    if _is_article_root(resolved_url, path):
        root_stem = path_segs[0] if path_segs else "article-root"
        score = max(score, 24)
        reasons.append(f"Corporate article/news root: '{root_stem}' (+24)")
        positive_signals.append(f"article-root:{root_stem}")

    # ── 5b. Announcements, Funding & News (+22) ─────────────────────────────
    news_path = _match_set_wb(ANNOUNCEMENT_NEWS_SIGNALS, path)
    news_anchor = _match_set_wb(ANNOUNCEMENT_NEWS_SIGNALS, anchor_lower)
    if news_path or news_anchor:
        signal = (news_path or news_anchor)[0]
        score = max(score, 22)
        reasons.append(f"Announcement/press signal: '{signal}' (+22)")
        positive_signals.append(f"news:{signal}")

    # ── 6. Careers & Culture (+20) ──────────────────────────────────────────
    career_match = _match_set_wb(CAREER_CULTURE_SIGNALS, f"{path} {anchor_lower}")
    if career_match:
        score = max(score, 20)
        reasons.append(f"Careers/culture signal: '{career_match[0]}' (+20)")
        positive_signals.append(f"careers:{career_match[0]}")

    # ── 7. Contact & Sales (+15) ────────────────────────────────────────────
    contact_match = _match_set_wb(CONTACT_SIGNALS, f"{path} {anchor_lower}")
    if contact_match:
        score = max(score, 15)
        reasons.append(f"Contact signal: '{contact_match[0]}' (+15)")

    # ── 8. Pricing & Plans (+8) ─────────────────────────────────────────────
    pricing_match = _match_set_wb(PRICING_SIGNALS, f"{path} {anchor_lower}")
    if pricing_match:
        score = max(score, 8)
        reasons.append(f"Pricing signal: '{pricing_match[0]}' (+8)")

    # ── 9. Author rel attribute (+25) ───────────────────────────────────────
    if "author" in rel_attr or _wb_match("author", path):
        score = max(score, 25)
        reasons.append("Author rel/path signal (+25)")
        positive_signals.append("author-rel")

    # ── 10. Multi-signal synergy bonus ──────────────────────────────────────
    has_pos_path = bool(lead_path or founders_path or people_path or about_path)
    has_pos_anchor = bool(lead_anchor or founders_anchor or people_anchor or about_anchor)
    if has_pos_path and has_pos_anchor:
        score += 2

    # ── Structural negative signals ─────────────────────────────────────────
    # Product/feature/technical/customer subpaths that should not be treated as leadership pages
    _PRODUCT_OR_CUSTOMER_SUBPATHS = (
        "/solutions/", "/solution/", "/products/", "/product/",
        "/docs/", "/guides/", "/templates/", "/catalog/",
        "/modules/", "/features/", "/integrations/", "/security/",
        "/industries/", "/settings", "/availability",
        "/customers/", "/customer/", "/customer-stories/", "/customer-story/",
        "/case-studies/", "/case-study/", "/clients/", "/client-stories/",
        "/partners/", "/partner-stories/", "/cookie",
    )
    if any(p in path for p in _PRODUCT_OR_CUSTOMER_SUBPATHS) or is_customer_case_study:
        if is_leadership:
            is_leadership = False
            negative_signals.append("product/customer subpath override")
        score = min(score, 8)
        reasons.append("Product/customer/technical subpath penalty (capped at 8)")
    # Careers and individual job posting subpaths should not receive leadership-level scores (+35)
    # (e.g. /careers/account-executive-... or /jobs/engineering-lead)
    if any(s in {"careers", "jobs"} for s in path_segs):
        score = min(score, 20)
        is_leadership = False

    # Final guard: corporate culture/diversity/policy, articles, products, platform UGC, or unknown single-segment routes can NEVER be is_leadership_relevant
    if is_non_leadership_topic or origin in ("article", "product", "platform_ugc", "unknown"):
        is_leadership = False

    if score > 0:
        return score, "; ".join(reasons), is_leadership, positive_signals, negative_signals

    # Non-empty unlisted internal path base score
    path_segments = [s for s in path.split("/") if s]
    if len(path_segments) == 1:
        return 3, "Single-segment internal path", False, [], []
    elif len(path_segments) == 2:
        return 2, "Two-segment internal path", False, [], []

    return 1, "Unlisted internal path", False, [], []



def score_link(
    resolved_url: str,
    anchor_text: str = "",
    title_text: str = "",
    surrounding_text: str = "",
    parent_heading: str = "",
    nav_context: str = "",
    rel_attr: str = "",
) -> int:
    """Score a candidate link using URL path, anchor text, and semantic context.

    Maintains full backward compatibility by returning an int.
    """
    score, _, _, _, _ = evaluate_link_relevance(
        resolved_url,
        anchor_text=anchor_text,
        title_text=title_text,
        surrounding_text=surrounding_text,
        parent_heading=parent_heading,
        nav_context=nav_context,
        rel_attr=rel_attr,
    )
    return score


def safe_get_attr(tag, key: str, default: str = "") -> str:
    """Safely get an attribute from a BeautifulSoup tag, handling None attrs or non-Tag elements."""
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


def extract_candidate_links(
    html: str,
    page_url: str,
    domain: str,
    base_url: str,
) -> list[CandidateLink]:
    """Extract and score internal links from page HTML using semantic link, anchor, heading, and parent context."""
    if not html or not isinstance(html, str):
        return []

    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[str, CandidateLink] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        resolved = resolve_url(page_url, href)
        if not resolved:
            continue

        if not is_same_domain(resolved, domain):
            continue

        anchor_text = a.get_text(" ", strip=True)
        title_text = safe_get_attr(a, "title") or safe_get_attr(a, "aria-label")
        rel_attr = safe_get_attr(a, "rel").lower()

        # Extract surrounding context (immediate parent container text)
        parent_container = a.find_parent(["li", "p", "div", "td", "article", "section"])
        surrounding_text = ""
        if parent_container:
            surrounding_text = parent_container.get_text(" ", strip=True)
            if len(surrounding_text) > 150:
                surrounding_text = surrounding_text[:150]

        # Extract parent navigation or section heading context
        nav = a.find_parent("nav")
        nav_label = ""
        if nav:
            nav_label = safe_get_attr(nav, "aria-label") or safe_get_attr(nav, "class")

        parent_heading = ""
        prev_h = a.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        if prev_h:
            h_text = prev_h.get_text(" ", strip=True)
            if len(h_text) < 100:
                parent_heading = h_text

        score, reason, is_leadership, _pos, _neg = evaluate_link_relevance(
            resolved,
            anchor_text=anchor_text,
            title_text=title_text,
            surrounding_text=surrounding_text,
            parent_heading=parent_heading,
            nav_context=nav_label,
            rel_attr=rel_attr,
        )

        if score > 0:
            cand = CandidateLink(
                url=resolved,
                score=score,
                reason=reason,
                is_leadership_relevant=is_leadership,
                anchor_text=anchor_text,
            )
            if resolved not in candidates or score > candidates[resolved].score:
                candidates[resolved] = cand

    # Return sorted by score descending
    sorted_candidates = sorted(candidates.values(), key=lambda x: x.score, reverse=True)
    return sorted_candidates


def extract_linkedin_links_from_html(html: str, page_url: str) -> list[dict]:
    """Extract direct LinkedIn profile/company URLs from page HTML with container context."""
    if not html or not isinstance(html, str):
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "linkedin.com/in/" in href.lower() or "linkedin.com/pub/" in href.lower() or "linkedin.com/company/" in href.lower():
            clean_url = href.split("?")[0].rstrip("/")
            if clean_url in seen:
                continue
            seen.add(clean_url)

            name = a.get_text(strip=True)
            if not name or len(name) < 2 or name.lower() in ["linkedin", "profile", "view linkedin"]:
                name = safe_get_attr(a, "title") or safe_get_attr(a, "aria-label")

            # Check parent container for rich context (card/list item/paragraph)
            container = a.find_parent(["div", "li", "article", "section", "p", "td"])
            container_text = container.get_text(" — ", strip=True) if container else ""
            if len(container_text) > 150:
                container_text = container_text[:150] + "..."

            context_str = name if (name and len(name) > 3) else container_text or "LinkedIn Profile"

            results.append({
                "url": clean_url,
                "context": context_str,
                "source_page": page_url,
            })
    return results


async def create_browser_context(browser: Browser) -> BrowserContext:
    """Create a stealth-like browser context."""
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    return context


async def fetch_page_html(page: Page, url: str) -> tuple[str | None, int, str]:
    """Fetch a single page and return (html, status_code, final_url)."""
    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=BROWSER_TIMEOUT_MS,
        )

        if response is None:
            logger.warning(f"  ⚠ No response from: {url}")
            return None, 0, url

        status = response.status
        if status >= 400:
            logger.warning(f"  ⚠ HTTP {status}: {url}")
            return None, status, page.url or url

        # Wait for dynamic JS content / hydration
        await page.wait_for_timeout(PAGE_WAIT_MS)

        try:
            await page.wait_for_selector("body", timeout=5000)
        except Exception:
            pass

        html = await page.content()
        final_url = page.url or url
        return html, status, final_url

    except Exception as e:
        error_name = type(e).__name__
        logger.warning(f"  ⚠ Failed to fetch {url}: {error_name} - {str(e)[:100]}")
        return None, 0, url


async def crawl_domain(domain: str) -> dict:
    """Crawl a domain's homepage and relevant subpages recursively up to depth 2.

    Returns dict containing:
      - 'pages': dict[url -> html]
      - 'crawl_stats': CrawlStats
      - 'linkedin_links': list[dict]
      - 'emails': list[str]
    """
    domain = normalize_domain(domain)
    base_url = domain_to_url(domain)

    results_pages: dict[str, str] = {}
    all_discovered_candidates: dict[str, CandidateLink] = {}
    page_titles: dict[str, str] = {}
    direct_linkedin_links: list[dict] = []
    discovered_emails: list[str] = []

    pages_failed_count = 0
    redirect_exclusions_count = 0
    platform_exclusions_count = 0

    logger.info(f"🌐 Crawling: {domain}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await create_browser_context(browser)
        page = await context.new_page()

        # Step 1: Fetch Homepage (Depth 0)
        logger.info(f"  📄 Fetching homepage: {base_url}")
        homepage_html, status, final_base_url = await fetch_page_html(page, base_url)

        if not homepage_html:
            logger.error(f"  ❌ Failed to fetch homepage for {domain}")
            await browser.close()
            return {
                "pages": {},
                "crawl_stats": CrawlStats(
                    pages_crawled=0,
                    pages_failed=1,
                    candidate_links_found=0,
                    relevant_links_selected=0,
                ),
                "linkedin_links": [],
                "emails": [],
            }

        results_pages[base_url] = homepage_html
        logger.info(f"  ✓ Homepage fetched ({len(homepage_html):,} chars)")

        try:
            h_soup = BeautifulSoup(homepage_html[:10000], "html.parser")
            if h_soup.title and h_soup.title.string:
                page_titles[base_url] = h_soup.title.string.strip()
        except Exception:
            pass

        # Direct evidence extraction from Homepage
        direct_linkedin_links.extend(extract_linkedin_links_from_html(homepage_html, base_url))
        discovered_emails.extend(extract_emails_from_text(homepage_html))

        # Discover candidates from Homepage
        homepage_candidates = extract_candidate_links(homepage_html, base_url, domain, base_url)
        for cand in homepage_candidates:
            all_discovered_candidates[cand.url] = cand

        # Add fallback common paths to discover key company and announcement pages
        # Fallback common paths with equivalence group awareness.
        # If an equivalent page was already discovered (e.g. /about already found),
        # do not add redundant fallback paths (e.g. /company, /about-us).
        fallback_paths = [
            ("/team", "Fallback team path", False, 18),
            ("/leadership", "Fallback leadership path", True, 30),
            ("/about", "Fallback about path", False, 18),
            ("/company", "Fallback company path", False, 18),
            ("/about-us", "Fallback about-us path", False, 18),
            ("/news", "Fallback news path", False, 20),
            ("/press", "Fallback press path", False, 20),
            ("/blog", "Fallback blog path", False, 20),
            ("/careers", "Fallback careers path", False, 15),
            ("/contact", "Fallback contact path", False, 12),
            ("/pricing", "Fallback pricing path", False, 8),
        ]

        # Equivalence categories for fallback pruning
        _EQUIV_GROUPS = [
            {"about", "about-us", "company"},
            {"news", "press", "blog"},
        ]

        discovered_canonical_stems = set()
        for u in all_discovered_candidates:
            cp = _canonical_path(u).strip("/")
            first_seg = cp.split("/")[0] if cp else ""
            if first_seg:
                discovered_canonical_stems.add(first_seg)

        added_fallback_stems = set()
        for fp, reason, is_lead, score in fallback_paths:
            constructed = f"{base_url}{fp}"
            stem = fp.strip("/")

            # Check if this stem or an equivalent is already discovered or added
            equiv_already_present = False
            for group in _EQUIV_GROUPS:
                if stem in group:
                    if any(g in discovered_canonical_stems or g in added_fallback_stems for g in group):
                        equiv_already_present = True
                        break

            if equiv_already_present and not is_lead:
                continue

            if constructed not in all_discovered_candidates:
                all_discovered_candidates[constructed] = CandidateLink(
                    url=constructed,
                    score=score,
                    reason=reason,
                    is_leadership_relevant=is_lead,
                    anchor_text=stem,
                )
                added_fallback_stems.add(stem)

        # Deduplicate locale-equivalent URLs before selection
        all_discovered_candidates = _deduplicate_candidates(all_discovered_candidates)

        leadership_pages_discovered = sum(1 for c in all_discovered_candidates.values() if c.is_leadership_relevant)
        sorted_candidates = sorted(
            all_discovered_candidates.values(),
            key=_candidate_sort_key,
            reverse=True,
        )
        logger.info(f"  🔍 DISCOVERED {len(sorted_candidates)} candidate links ({leadership_pages_discovered} leadership-related):")
        for cand in sorted_candidates[:12]:
            lead_tag = " [LEADERSHIP]" if cand.is_leadership_relevant else ""
            fallback_tag = " [FALLBACK]" if cand.reason.startswith("Fallback") else ""
            logger.info(f"    {cand.url} [score={cand.score}]{lead_tag}{fallback_tag} ({cand.reason})")

        # Dynamic Crawl Frontier:
        # Instead of statically freezing the subpages before crawling, we maintain
        # a priority queue. If a crawled page discovers a high-confidence leadership page
        # (e.g. /about -> /about/leadership), that genuine leadership page immediately
        # jumps to the front of the queue ahead of generic corporate, news, or fallback pages.
        crawl_frontier: list[str] = [cand.url for cand in sorted_candidates if cand.score > 0]
        selected_candidates: list[str] = []
        max_total_pages = MAX_SUBPAGES + 1

        while crawl_frontier and len(results_pages) < max_total_pages:
            url = crawl_frontier.pop(0)
            canon = _canonical_path(url)
            crawled_canons = {_canonical_path(u) for u in results_pages}
            if url in results_pages or canon in crawled_canons:
                continue

            cand = all_discovered_candidates.get(url)
            lead_tag = " [LEADERSHIP]" if cand and cand.is_leadership_relevant else ""
            logger.info(f"  📄 Fetching: {urlparse(url).path or url}{lead_tag}")
            await asyncio.sleep(CRAWL_DELAY_S)

            html, status, final_url = await fetch_page_html(page, url)
            if not html:
                pages_failed_count += 1
                continue

            final_canon = _canonical_path(final_url)
            base_canon = _canonical_path(base_url)

            # ── Redirect Analysis & Slot Protection ────────────────────────
            # 1. Homepage redirect (e.g. stripe.com/team -> stripe.com/in)
            if final_canon == base_canon:
                logger.info(f"  ↪ Redirect excluded: {url} resolved to homepage ({final_url})")
                redirect_exclusions_count += 1
                continue

            # 2. Auth/login or platform route redirect
            final_origin = _classify_url_origin(final_url)
            if final_origin == "auth":
                logger.info(f"  ↪ Redirect excluded: {url} resolved to auth route ({final_url})")
                redirect_exclusions_count += 1
                continue
            elif final_origin == "platform_ugc":
                logger.info(f"  ↪ Origin excluded: {url} resolved to platform_ugc route ({final_url})")
                platform_exclusions_count += 1
                continue

            # 3. External domain redirect
            if not is_same_domain(final_url, domain):
                logger.info(f"  ↪ Redirect excluded: {url} resolved to external domain ({final_url})")
                redirect_exclusions_count += 1
                continue

            # 4. Already-crawled canonical destination
            if final_canon in crawled_canons or final_url in results_pages:
                logger.info(f"  ↪ Redirect excluded: {url} resolved to already-crawled destination ({final_url})")
                redirect_exclusions_count += 1
                continue

            # 5. Page content identity check (handles platforms hosting user profiles/repos)
            page_identity = _inspect_page_content_identity(html, url, final_url)
            if page_identity == "auth":
                logger.info(f"  ↪ Content excluded: {url} resolved to authentication page ({final_url})")
                redirect_exclusions_count += 1
                continue
            elif page_identity == "platform_ugc":
                logger.info(f"  ↪ Content excluded: {url} resolved to platform/user-generated content ({final_url})")
                platform_exclusions_count += 1
                continue

            # 6. Fallback intent verification:
            # If a speculative fallback candidate was added specifically for leadership (e.g. /team or /leadership),
            # verify that it actually resolved to leadership/people content, not an unrelated or empty page.
            if cand and cand.reason.startswith("Fallback") and cand.is_leadership_relevant:
                has_leadership_content = (
                    page_identity == "corporate_leadership"
                    or any(w in html[:40000].lower() for w in (
                        "executive leadership", "leadership team", "board of directors",
                        "executive officers", "our leadership", "meet our team", "management team",
                        "chief executive", "chief technology"
                    ))
                )
                if not has_leadership_content:
                    logger.info(f"  ↪ Fallback excluded: {url} resolved to content without leadership evidence ({final_url})")
                    redirect_exclusions_count += 1
                    continue

            # Legitimate destination: store under final_url
            results_pages[final_url] = html
            if final_url not in selected_candidates and url not in selected_candidates:
                selected_candidates.append(final_url)
            logger.debug(f"    ✓ Fetched ({len(html):,} chars)")

            try:
                p_soup = BeautifulSoup(html[:10000], "html.parser")
                if p_soup.title and p_soup.title.string:
                    page_titles[final_url] = p_soup.title.string.strip()
            except Exception:
                pass

            # Extract evidence
            direct_linkedin_links.extend(extract_linkedin_links_from_html(html, final_url))
            discovered_emails.extend(extract_emails_from_text(html))

            # Discover new candidate links from this page
            sub_candidates = extract_candidate_links(html, final_url, domain, base_url)
            new_high_value = False
            for sub_cand in sub_candidates:
                prev = all_discovered_candidates.get(sub_cand.url)
                if prev is None or sub_cand.score > prev.score:
                    all_discovered_candidates[sub_cand.url] = sub_cand

                sub_canon = _canonical_path(sub_cand.url)
                if (
                    sub_cand.url not in results_pages
                    and sub_canon not in crawled_canons
                    and (sub_cand.is_leadership_relevant or sub_cand.score >= 20)
                ):
                    if sub_cand.url not in crawl_frontier:
                        crawl_frontier.append(sub_cand.url)
                        new_high_value = True

            # If new candidates were discovered, re-sort frontier so genuine leadership pages take priority
            if new_high_value:
                crawl_frontier.sort(
                    key=lambda u: _candidate_sort_key(all_discovered_candidates.get(u, CandidateLink(u, 0))),
                    reverse=True,
                )

        await browser.close()

    # Deduplicate direct LinkedIn links and emails
    unique_linkedin = []
    seen_linkedin = set()
    for link in direct_linkedin_links:
        if link["url"] not in seen_linkedin:
            seen_linkedin.add(link["url"])
            unique_linkedin.append(link)

    unique_emails = list(dict.fromkeys(discovered_emails))

    # Build detailed SelectedPageInfo list for observability
    selected_page_infos = []
    for u in results_pages.keys():
        cand = all_discovered_candidates.get(u)
        is_lead = cand.is_leadership_relevant if cand else False
        html_content = results_pages.get(u, "")
        fetched_id = _inspect_page_content_identity(html_content, u, u)
        if fetched_id in ("product", "article", "platform_ugc"):
            is_lead = False
        selected_page_infos.append(SelectedPageInfo(
            url=u,
            score=cand.score if cand else (0 if u == base_url else 10),
            reason=cand.reason if cand else ("Homepage" if u == base_url else "Direct crawl"),
            title=page_titles.get(u),
            is_leadership_relevant=is_lead,
        ))

    total_leadership_pages = sum(1 for c in all_discovered_candidates.values() if c.is_leadership_relevant)

    crawl_stats = CrawlStats(
        pages_crawled=len(results_pages),
        pages_failed=pages_failed_count,
        candidate_links_found=len(all_discovered_candidates),
        relevant_links_selected=len(selected_candidates),
        leadership_pages_found=total_leadership_pages,
        selected_pages=selected_page_infos,
    )

    logger.info(
        f"  ✅ Crawl complete: {len(results_pages)} pages fetched for {domain} "
        f"({pages_failed_count} failed, {len(all_discovered_candidates)} candidates discovered, "
        f"{total_leadership_pages} leadership-relevant discovered, "
        f"{redirect_exclusions_count} redirect exclusions, {platform_exclusions_count} platform/UGC exclusions)"
    )

    return {
        "pages": results_pages,
        "crawl_stats": crawl_stats,
        "linkedin_links": unique_linkedin,
        "emails": unique_emails,
        "redirect_exclusions": redirect_exclusions_count,
        "platform_exclusions": platform_exclusions_count,
    }
