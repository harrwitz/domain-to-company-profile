"""
search_fallback.py — LinkedIn Employment Verification & Enrichment

When key leadership members are found but lack LinkedIn URLs, this module
uses the Tavily search API to look them up and verify employment.

This implements a generalized pipeline:
  1. Search LinkedIn for each candidate
  2. Verify identity match (name, role)
  3. Verify current employer matches target company
  4. Reject candidates whose LinkedIn shows a different current employer
"""

from dataclasses import dataclass
import asyncio
import re

from config import TAVILY_API_KEY, MAX_LINKEDIN_LOOKUPS, SEARCH_DELAY_S
from models import TeamMember
from utils import logger


@dataclass
class VerificationMetrics:
    """Metrics tracking for candidate verification and LinkedIn lookups."""
    candidates_found: int = 0
    candidates_verified: int = 0
    candidates_rejected: int = 0
    searches_performed: int = 0
    matches_found: int = 0


metrics = VerificationMetrics()


def get_verification_metrics() -> VerificationMetrics:
    """Get the current verification metrics."""
    return metrics


def is_tavily_available() -> bool:
    """Check if Tavily API key is configured."""
    return bool(TAVILY_API_KEY) and not TAVILY_API_KEY.startswith("tvly-your")


# ─── Organization Normalization ──────────────────────────────────────────────────

def normalize_org_name(org: str) -> str:
    """Normalize organization name for fuzzy matching.

    Handles:
    - Inc., Ltd., LLC, Corp, Co. suffixes
    - Punctuation
    - Domain-to-company matching (e.g., 'supabase.com' → 'supabase')
    - Case normalization

    Examples:
        'Supabase Inc.' → 'supabase'
        'Postman, Inc.' → 'postman'
        'Vapi.ai' → 'vapi'
        'example.com' → 'example'
    """
    if not org:
        return ""
    org = org.strip().lower()
    # Strip URL-like prefixes
    org = re.sub(r"^https?://", "", org)
    org = re.sub(r"^www\.", "", org)
    # Strip path and port after domain
    org = org.split("/")[0].split(":")[0]
    # Remove domain extensions (.com, .org, .io, .ai, etc.)
    org = re.sub(r"\.(com|org|net|ai|io|app|co|dev|tech|xyz|so)\b", "", org)
    # Remove common corporate suffixes
    org = re.sub(
        r"\b(inc\.?|corp\.?|corporation|llc|ltd\.?|limited|co\.?|company|"
        r"platform|technologies|software)\b",
        "", org
    )
    # Remove all non-alphanumeric characters
    org = re.sub(r"[^\w]", "", org)
    return org.strip()


def orgs_match(org_a: str, org_b: str) -> bool:
    """Check if two organization names refer to the same company.

    Uses normalized comparison and substring matching for parent/subsidiary names.
    """
    norm_a = normalize_org_name(org_a)
    norm_b = normalize_org_name(org_b)

    if not norm_a or not norm_b:
        return False

    # Exact match after normalization
    if norm_a == norm_b:
        return True

    # One contains the other (handles "Supabase" vs "Supabase Platform")
    if norm_a in norm_b or norm_b in norm_a:
        return True

    return False


def _extract_employer_from_snippet(snippet: str, target_company: str) -> str | None:
    """Try to extract the current employer from a LinkedIn search snippet.

    Looks for patterns like:
    - "Title at Company"
    - "Company · Present"
    - "Current: Title at Company"
    """
    if not snippet:
        return None

    target_norm = normalize_org_name(target_company)

    # Pattern: "at Company" or "@ Company"
    at_patterns = re.findall(r"(?:at|@)\s+([A-Z][A-Za-z0-9\.\-\s]+?)(?:\s*[·|–—\-]|\s*$|,|\.|;)", snippet)
    for match in at_patterns:
        match = match.strip()
        if match and len(match) > 1:
            if orgs_match(match, target_company):
                return "TARGET"
            return match

    # Pattern: "Company · Present" or "Company - Present"
    present_patterns = re.findall(r"([A-Z][A-Za-z0-9\.\-\s]+?)\s*[·|–—\-]\s*(?:Present|Current|Now)", snippet, re.IGNORECASE)
    for match in present_patterns:
        match = match.strip()
        if match and len(match) > 1:
            if orgs_match(match, target_company):
                return "TARGET"
            return match

    # Check if target company appears prominently
    if target_norm and target_norm in normalize_org_name(snippet):
        return "TARGET"

    return None


def _verify_linkedin_profile_identity(target_name: str, url: str, title: str, snippet: str) -> bool:
    """Verify that a candidate LinkedIn profile corresponds to the target person's name.

    Distinguishes the actual profile holder from search results that merely mention
    someone else or another person at the same company.

    Requirements:
    - Surname must appear in the profile title (before company/role) or the URL slug.
    - First name (or common diminutive like Mike/Michael, Dave/David, etc.) or first initial must match.
    - Rejects profile results where the profile belongs to someone else entirely (e.g. searching
      for Mike Clayville returning John Collison's profile).
    """
    if not url or "linkedin.com/in/" not in url.lower():
        return False

    name_parts = [p.lower() for p in target_name.strip().split() if p.strip()]
    if not name_parts:
        return False

    first_name = name_parts[0]
    last_name = name_parts[-1] if len(name_parts) > 1 else ""

    # 1. Extract profile holder name from title
    # Formats: "First Last - Title - Company | LinkedIn", "First Last | LinkedIn"
    title_clean = title.strip()
    profile_name_raw = re.split(r'[-–—|:]', title_clean)[0].strip().lower()
    profile_name_raw = re.sub(r'\(.*?\)', '', profile_name_raw)
    profile_name_raw = re.sub(r'\b(?:phd|ph\.d\.|cpa|mba|md|esq)\b', '', profile_name_raw)
    profile_parts = [p.strip(" ,.") for p in profile_name_raw.split() if p.strip(" ,.")]

    # 2. Extract profile slug from URL: linkedin.com/in/johnbcollison -> johnbcollison
    slug_match = re.search(r'linkedin\.com/in/([^/?#]+)', url.lower())
    slug = slug_match.group(1) if slug_match else ""
    slug_alpha = re.sub(r'[-_\d]', '', slug)

    # 3. Surname verification:
    if last_name:
        surname_in_title = last_name in profile_parts or last_name in profile_name_raw
        surname_in_slug = last_name in slug_alpha or last_name in slug.lower()
        if not (surname_in_title or surname_in_slug):
            return False

    # 4. First name / diminutive / initial verification:
    nicknames = {
        "mike": ["michael", "mike"],
        "michael": ["mike", "michael"],
        "dave": ["david", "dave"],
        "david": ["dave", "david"],
        "tom": ["thomas", "tom"],
        "thomas": ["tom", "thomas"],
        "bob": ["robert", "bob", "bobby"],
        "robert": ["bob", "robert"],
        "bill": ["william", "bill", "billy"],
        "william": ["bill", "william"],
        "dan": ["daniel", "dan"],
        "daniel": ["dan", "daniel"],
        "chris": ["christopher", "chris"],
        "christopher": ["chris", "christopher"],
        "jim": ["james", "jim", "jimmy"],
        "james": ["jim", "james"],
    }
    allowed_first_names = set(nicknames.get(first_name, [first_name]))

    first_in_title = any(fn in profile_parts or fn in profile_name_raw for fn in allowed_first_names)
    first_in_slug = any(fn in slug_alpha for fn in allowed_first_names)
    initial_in_slug = slug.startswith(first_name[0]) or f"-{first_name[0]}-" in slug

    if not (first_in_title or first_in_slug or initial_in_slug):
        return False

    return True


def search_and_verify_linkedin(
    name: str,
    role: str,
    company_domain: str,
) -> dict:
    """Search LinkedIn and verify employment for a candidate (Rule 4).

    Returns a dict with:
    - linkedin_url: str | None
    - verified: bool (True if LinkedIn confirms target company employment)
    - employer: str | None (current employer from LinkedIn, if found)
    - rejected: bool (True if LinkedIn shows a *different* current employer)
    - reason: str (explanation of the decision)
    """
    result = {
        "linkedin_url": None,
        "verified": False,
        "employer": None,
        "rejected": False,
        "reason": "No LinkedIn search performed",
    }

    if not is_tavily_available():
        result["reason"] = "Tavily API not configured"
        return result

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)

        company_name = company_domain.split(".")[0].capitalize()

        # Rule 4: Generic search strategies
        queries = [
            f'site:linkedin.com/in "{name}" "{company_name}"',
            f'site:linkedin.com/in "{name}" "{role}" "{company_name}"',
            f'site:linkedin.com/in "{name}" "{role}"',
            f'site:linkedin.com/in "{name}"',
        ]

        best_url = None
        best_snippet = None

        for query in queries:
            try:
                metrics.searches_performed += 1
                response = client.search(
                    query=query,
                    max_results=3,
                    include_domains=["linkedin.com"],
                )

                for search_result in response.get("results", []):
                    url = search_result.get("url", "")
                    title = search_result.get("title", "")
                    snippet = search_result.get("content", "") or title

                    if "linkedin.com/in/" not in url.lower():
                        continue

                    # Verify profile identity corresponds to the target person's name
                    if not _verify_linkedin_profile_identity(name, url, title, snippet):
                        continue

                    if not best_url:
                        best_url = url
                        best_snippet = snippet
                        metrics.matches_found += 1
                    break

            except Exception:
                continue

            if best_url:
                break

        if not best_url:
            result["reason"] = "No LinkedIn profile found for this person"
            return result

        result["linkedin_url"] = best_url

        # Verify employer from snippet
        employer = _extract_employer_from_snippet(best_snippet or "", company_domain)

        if employer == "TARGET":
            result["verified"] = True
            result["employer"] = company_name
            result["reason"] = f"LinkedIn confirms current employment at {company_name}"
            metrics.candidates_verified += 1
        elif employer:
            result["rejected"] = True
            result["employer"] = employer
            result["reason"] = f"LinkedIn shows different current employer: {employer}"
            metrics.candidates_rejected += 1
        else:
            # Ambiguous: can't determine employer from snippet
            result["reason"] = "LinkedIn profile found but could not verify current employer"

        return result

    except Exception as e:
        result["reason"] = f"LinkedIn search failed: {str(e)[:100]}"
        return result


async def enrich_linkedin_urls(
    members: list[TeamMember],
    company_domain: str,
) -> list[TeamMember]:
    """Enrich team members with LinkedIn URLs via search and verify employment.

    For members without LinkedIn URLs:
    - Search LinkedIn
    - Verify that the current employer matches the target company
    - Reject members whose LinkedIn shows a different employer

    Args:
        members: List of TeamMember objects to enrich
        company_domain: The company domain (for search context)

    Returns:
        Updated list of TeamMember objects (with verified-only LinkedIn URLs)
    """
    if not is_tavily_available():
        logger.info("  ⏭ LinkedIn enrichment skipped (TAVILY_API_KEY not set)")
        return members

    # Find members needing LinkedIn URLs
    needs_linkedin = [m for m in members if not m.linkedin_url]

    if not needs_linkedin:
        logger.info("  ✓ All team members already have LinkedIn URLs")
        return members

    lookups = min(len(needs_linkedin), MAX_LINKEDIN_LOOKUPS)
    logger.info(
        f"🔍 Searching LinkedIn URLs for {lookups} team member(s) "
        f"at {company_domain}"
    )

    company_name = company_domain.split(".")[0].capitalize()
    verified_members = []
    rejected_members = []

    for i, member in enumerate(needs_linkedin[:lookups]):
        if i > 0:
            await asyncio.sleep(SEARCH_DELAY_S)  # Rate limiting

        linkedin_result = await asyncio.to_thread(
            search_and_verify_linkedin,
            name=member.name,
            role=member.role,
            company_domain=company_domain,
        )

        if linkedin_result["verified"]:
            member.linkedin_url = linkedin_result["linkedin_url"]
            member.linkedin_source = "search_verified"
            verified_members.append(member.name)
            logger.info(f"  ✓ LinkedIn verified for {member.name} at {company_name}")
        elif linkedin_result["rejected"]:
            rejected_members.append(member.name)
            logger.info(
                f"  ✗ LinkedIn REJECTED {member.name}: {linkedin_result['reason']}"
            )
        else:
            logger.debug(f"  ? LinkedIn ambiguous for {member.name}: {linkedin_result['reason']}")

    # Remove rejected members from the list
    if rejected_members:
        members = [m for m in members if m.name not in rejected_members]
        logger.info(f"  ❌ Removed {len(rejected_members)} member(s) with different LinkedIn employers")

    return members
