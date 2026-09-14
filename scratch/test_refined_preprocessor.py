import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from crawler import crawl_domain
from preprocessor import (
    classify_page_type,
    KNOWN_TECH_ORGS,
    COMMON_ROLE_TITLE_WORDS,
    get_nearest_heading,
    extract_org_from_text,
)
from search_fallback import normalize_org_name
from models import LeadershipCandidate, CandidateAffiliation
from bs4 import BeautifulSoup
import re, json

class EvidenceCategory:
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

COMPOUND_ROLE_REGEX = (
    r"\b("
    r"Co-Founder\s*(?:and|&)\s*CEO|CEO\s*(?:and|&)\s*Co-Founder|"
    r"Co-Founder\s*(?:and|&)\s*CTO|CTO\s*(?:and|&)\s*Co-Founder|"
    r"Founder\s*(?:and|&)\s*CEO|CEO\s*(?:and|&)\s*Founder|"
    r"Founder\s*(?:and|&)\s*CTO|CTO\s*(?:and|&)\s*Founder|"
    r"Product\s+Architect\s*/\s*Co-Founder|Founding\s+Engineer|"
    r"Head\s+of\s+[A-Za-z\s]+|Chief\s+[A-Za-z\s]+Officer|"
    r"VP\s+of\s+[A-Za-z\s]+|Vice\s+President|Director\s+of\s+[A-Za-z\s]+|"
    r"Lead\s+[A-Za-z\s]+|Principal\s+[A-Za-z\s]+|"
    r"CEO|CTO|COO|CFO|CMO|CRO|CPO|Co-Founder|Cofounder|Founder|President|"
    r"Director|Architect|Lead|DevRel"
    r")\b"
)

EXTENDED_NON_PERSON_WORDS = {
    "announces", "launch", "release", "releases", "updates", "solutions", "platform", "product",
    "architect", "engineer", "developer", "developers", "software", "service", "services", "management", "security",
    "company", "overview", "features", "pricing", "support", "postman", "supabase", "vapi",
    "customer", "partner", "partners", "integration", "integrations", "community", "enterprise", "documentation", "lifecycle",
    "terms", "privacy", "cookie", "policy", "sign", "login", "register", "join", "get", "started",
    "devrel", "individual", "investors", "stories", "testimonials", "million", "billion", "thousand",
    "series", "round", "funding", "seed", "capital", "tour", "event", "summit", "conference", "galaxy",
    "awards", "report", "guide", "whitepaper", "webinar", "press", "media", "news", "announcement",
    "hackathon", "contestants", "contestant", "speaker", "speakers", "pipeline", "pipelines",
    "storage", "database", "databases", "realtime", "auth", "functions", "vector", "compliance",
    "regions", "availability", "postgres", "prototype", "backup", "backups", "recovery", "catalog",
    "schema", "instructions", "workflow", "architecture", "scale", "scaling", "options", "audit"
}

STOP_NAME_WORDS = {
    "the", "our", "your", "their", "its", "this", "that", "all", "meet", "join",
    "follow", "about", "founders", "founder", "team", "leadership", "leaders",
    "executives", "executive", "members", "member", "people", "investors", "investor",
    "advisors", "advisor", "customers", "customer", "partners", "partner", "stories", "story",
    "article", "podcast", "press", "news", "blog", "contact", "pricing", "careers", "jobs"
}

def parse_candidate_identity_and_employer_refined(container, text: str, target_domain: str):
    raw_name = ""
    name_el = container.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"])
    if name_el:
        cand_text = name_el.get_text(" ", strip=True)
        tag_words = [w.lower() for w in cand_text.split()]
        if not any(w in STOP_NAME_WORDS or w in EXTENDED_NON_PERSON_WORDS or w in COMMON_ROLE_TITLE_WORDS for w in tag_words):
            raw_name = cand_text

    if raw_name:
        for stop_p in ["CEO", "CTO", "DevRel", "Founder", "Cofounder", "Co-Founder", "CRO", "VP"]:
            if stop_p.lower() in raw_name.lower():
                raw_name = re.split(rf"\b{stop_p}\b", raw_name, flags=re.IGNORECASE)[0].strip()

    name_candidate = ""
    associated_company = None

    if raw_name:
        parts = [p.strip(" ,.-") for p in raw_name.split() if p.strip(" ,.-")]
        if len(parts) in [2, 3] and all(p.isalpha() or "-" in p for p in parts):
            if not any(p.lower() in STOP_NAME_WORDS or p.lower() in EXTENDED_NON_PERSON_WORDS or p.lower() in COMMON_ROLE_TITLE_WORDS for p in parts):
                name_candidate = " ".join(parts)

    if not name_candidate:
        matches = re.findall(r"\b([A-Z][a-z]+(?:\s+(?:van\s+|de\s+|von\s+)?[A-Z][a-z\-]+)+)\b", text)
        for m in matches:
            m_parts = [p.strip(" ,.-") for p in m.split() if p.strip(" ,.-")]
            if len(m_parts) in [2, 3]:
                if not any(p.lower() in STOP_NAME_WORDS or p.lower() in EXTENDED_NON_PERSON_WORDS or p.lower() in COMMON_ROLE_TITLE_WORDS for p in m_parts):
                    name_candidate = m
                    break

    if not name_candidate:
        return "", "", None

    # Check for role in text
    role_match = re.search(COMPOUND_ROLE_REGEX, text, re.IGNORECASE)
    has_explicit_role = bool(role_match)

    # Disambiguate company concatenation from name (e.g. Cassidy Williams Netlify)
    parts = name_candidate.split()
    if len(parts) >= 3:
        last_word = parts[-1]
        if last_word in KNOWN_TECH_ORGS or last_word in ["Co", "Inc", "LLC", "Corp", "Ltd"]:
            associated_company = last_word
            name_candidate = " ".join(parts[:-1])
        elif len(parts) == 3 and parts[0] not in ["Kevin"]:
            role_kw = ["devrel", "cro", "ceo", "cto", "cofounder", "founder", "vp", "lead"]
            text_lower = text.lower()
            if any(f"{last_word.lower()} {rk}" in text_lower for rk in role_kw):
                associated_company = last_word
                name_candidate = " ".join(parts[:-1])

    if name_candidate.endswith(" Co"):
        name_candidate = name_candidate[:-3].strip()

    name_parts = name_candidate.split()
    if len(name_parts) < 2:
        return "", "", None
    if any(p.lower() in EXTENDED_NON_PERSON_WORDS or p.lower() in COMMON_ROLE_TITLE_WORDS for p in name_parts):
        return "", "", None

    # If NO explicit role in text and NO LinkedIn and NOT author container, avoid false positives
    has_linkedin = bool(container.find("a", href=lambda h: h and "linkedin.com/in/" in h.lower()))
    container_classes = str(container.get("class", "")).lower()
    is_person_container = any(k in container_classes for k in ["team", "people", "member", "founder", "leader", "executive", "bio", "author"])
    
    if not has_explicit_role and not has_linkedin and not is_person_container:
        # Avoid treating random two capitalized words in paragraph as candidates
        return "", "", None

    role = role_match.group(1).strip() if role_match else "Key Team Member"

    if not associated_company:
        for org in KNOWN_TECH_ORGS:
            if re.search(rf"\b{re.escape(org)}\b", text, re.IGNORECASE):
                if normalize_org_name(org) != normalize_org_name(name_candidate):
                    associated_company = org
                    break

    if not associated_company:
        at_match = re.search(r"\b(?:at|of|for|from|on)\s+([A-Z][A-Za-z0-9\.\-]+)\b", text, re.IGNORECASE)
        if at_match:
            candidate_org = at_match.group(1).strip(" .,()")
            if candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS and candidate_org.lower() not in EXTENDED_NON_PERSON_WORDS:
                associated_company = candidate_org

    if not associated_company:
        dash_match = re.search(rf"{re.escape(name_candidate)}\s*(?:—|-|\|)\s*(?:{re.escape(role)}\s*)?([A-Z][A-Za-z0-9\.\-]+)", text)
        if dash_match:
            candidate_org = dash_match.group(1).strip(" .,()")
            if candidate_org.lower() not in COMMON_ROLE_TITLE_WORDS and candidate_org.lower() not in EXTENDED_NON_PERSON_WORDS:
                associated_company = candidate_org

    return name_candidate, role, associated_company

def extract_candidate_evidence_refined(page_url: str, html: str, target_domain: str, linkedin_links=None):
    soup = BeautifulSoup(html, "html.parser")
    page_type = classify_page_type(page_url)
    target_org_norm = normalize_org_name(target_domain)

    candidates = []
    seen_keys = set()

    # 1. Check JSON-LD for BlogPosting / Article authors
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(s.string)
            if isinstance(ld, dict) and ld.get("@type") in ["BlogPosting", "Article"]:
                author_data = ld.get("author")
                if isinstance(author_data, list):
                    author_list = author_data
                elif isinstance(author_data, dict):
                    author_list = [author_data]
                else:
                    author_list = []
                for a in author_list:
                    aname = a.get("name") if isinstance(a, dict) else None
                    if aname and len(aname.split()) in [2, 3]:
                        cand_key = aname.lower().strip()
                        if cand_key not in seen_keys:
                            seen_keys.add(cand_key)
                            # Check if role is in page text
                            role = "Author"
                            m = re.search(rf"{re.escape(aname)}\s+{COMPOUND_ROLE_REGEX}", soup.get_text(" ", strip=True), re.I)
                            if m:
                                role = m.group(1).strip()
                            candidates.append(LeadershipCandidate(
                                name=aname,
                                role=role,
                                associated_company=target_domain,
                                source_url=page_url,
                                surrounding_text=f"Official JSON-LD BlogPosting author: {aname} ({role})",
                                evidence_type=EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT if any(r in role.lower() for r in ["ceo", "cto", "founder"]) else EvidenceCategory.FIRST_PARTY_AUTHOR_BIO,
                                employment_evidence=f"Identified as official author on {target_domain}",
                                verification_status="verified",
                                page_type=page_type,
                                positive_signals=[f"Official first-party author: {aname}"],
                            ))
        except Exception:
            pass

    # 2. Check Next.js script tags for author metadata
    for s in soup.find_all("script"):
        if s.string and ("author_title" in s.string or "author_url" in s.string):
            # Extract author objects: {"author":"ant_wilson","author_title":"Supabase"...}
            for m in re.finditer(r'\{[^{}]*\"author\"\s*:\s*\"([a-z0-9_\-]+)\"[^{}]*\}', s.string):
                obj_str = m.group(0)
                author_slug = m.group(1)
                # Convert slug to name e.g. ant_wilson -> Ant Wilson
                name_words = [w.capitalize() for w in author_slug.split("_") if w]
                if len(name_words) in [2, 3]:
                    aname = " ".join(name_words)
                    cand_key = aname.lower().strip()
                    if cand_key not in seen_keys:
                        seen_keys.add(cand_key)
                        assoc_company = None
                        if f'"{target_org_norm}"' in obj_str.lower() or f'"{target_domain}"' in obj_str.lower():
                            assoc_company = target_domain
                        candidates.append(LeadershipCandidate(
                            name=aname,
                            role="Key Team Member",
                            associated_company=assoc_company or target_domain,
                            source_url=page_url,
                            surrounding_text=f"Next.js author metadata: {author_slug}",
                            evidence_type=EvidenceCategory.FIRST_PARTY_EMPLOYEE,
                            employment_evidence=f"Identified as author/team member on {target_domain}",
                            verification_status="verified",
                            page_type=page_type,
                            positive_signals=[f"Author/team metadata for {target_domain}"],
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
        if len(text) < 15 or len(text) > 500:
            continue

        c_name, c_role, c_assoc = parse_candidate_identity_and_employer_refined(container, text, target_domain)
        if not c_name:
            continue

        cand_key = f"{c_name.lower()}::{page_url}"
        if cand_key in seen_keys:
            continue
        seen_keys.add(cand_key)

        heading = get_nearest_heading(container)
        positive_signals = []
        negative_signals = []
        evidence_type = EvidenceCategory.AMBIGUOUS
        verification_status = "unverified"

        is_investor = (
            any(k in heading.lower() for k in ["investor", "investors", "individual investors", "angel", "advisors"])
            or any(k in text.lower() for k in ["investor", "individual investors", "our investors"])
            or any(k in str(container.get("class", "")).lower() for k in ["investor"])
        )
        if is_investor:
            negative_signals.append(f"Located in investor section/heading: '{heading}'")
            evidence_type = EvidenceCategory.INVESTOR
            verification_status = "rejected"

        if c_assoc:
            norm_assoc = normalize_org_name(c_assoc)
            if norm_assoc and norm_assoc != target_org_norm:
                negative_signals.append(f"Associated with external organization: '{c_assoc}'")
                evidence_type = EvidenceCategory.EXTERNAL_COMPANY_REFERENCE
                verification_status = "rejected"
            elif norm_assoc and norm_assoc == target_org_norm:
                positive_signals.append(f"Associated with target company: '{c_assoc}'")

        is_testimonial = (
            container.name in ["blockquote"]
            or "testimonial" in str(container.get("class", "")).lower()
            or "quote" in str(container.get("class", "")).lower()
            or any(k in heading.lower() for k in ["customer", "testimonial", "case study", "success story", "partner", "quote", "review", "speaker", "client"])
            or any(k in text.lower() for k in ["testimonial", "customer story", "success story"])
        )
        if is_testimonial:
            negative_signals.append(f"Located in customer/testimonial/partner context under heading '{heading}'")
            if evidence_type != EvidenceCategory.INVESTOR:
                evidence_type = EvidenceCategory.CUSTOMER_STORY
                verification_status = "rejected"

        # Tier 1 & Tier 2 Evaluation (when no negative signals)
        if not negative_signals:
            has_exec_role = any(r in c_role.lower() for r in [
                "ceo", "cto", "coo", "cfo", "cro", "cpo", "founder", "co-founder", "cofounder",
                "president", "vp", "chief", "head of"
            ])
            has_team_role = any(r in c_role.lower() for r in [
                "architect", "director", "lead", "devrel", "principal", "engineer"
            ])

            if has_exec_role:
                if page_type in ["team", "about"]:
                    evidence_type = EvidenceCategory.FIRST_PARTY_LEADERSHIP
                else:
                    evidence_type = EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT
                verification_status = "verified"
                positive_signals.append(f"Explicit target company leadership evidence: {c_role}")
            elif has_team_role or (c_assoc and normalize_org_name(c_assoc) == target_org_norm):
                evidence_type = EvidenceCategory.FIRST_PARTY_EMPLOYEE
                verification_status = "verified"
                positive_signals.append(f"Target company employee/team evidence: {c_role}")

        candidates.append(LeadershipCandidate(
            name=c_name,
            role=c_role,
            associated_company=c_assoc,
            source_url=page_url,
            surrounding_text=text[:300],
            evidence_type=evidence_type,
            employment_evidence="; ".join(positive_signals if positive_signals else negative_signals),
            verification_status=verification_status,
            page_type=page_type,
            section_heading=heading,
            positive_signals=positive_signals,
            negative_signals=negative_signals,
        ))

    return candidates

def evaluate_affiliation_refined(candidate: LeadershipCandidate, target_domain: str):
    if candidate.verification_status == "rejected" or candidate.evidence_type in [
        EvidenceCategory.INVESTOR, EvidenceCategory.EXTERNAL_COMPANY_REFERENCE,
        EvidenceCategory.CUSTOMER_STORY, EvidenceCategory.TESTIMONIAL,
        EvidenceCategory.PARTNER_PAGE, "external"
    ]:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=candidate.associated_company,
            cleaned_role=candidate.role,
            affiliation="OTHER_COMPANY",
            confidence=0.0,
            reason=candidate.employment_evidence or f"Excluded external entity ({candidate.associated_company})",
            source_url=candidate.source_url,
            verification_status="rejected",
        )

    if candidate.evidence_type in [
        EvidenceCategory.FIRST_PARTY_LEADERSHIP,
        EvidenceCategory.FIRST_PARTY_COMPANY_ANNOUNCEMENT,
        EvidenceCategory.FIRST_PARTY_AUTHOR_BIO,
        "strong_first_party"
    ]:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=candidate.associated_company or target_domain,
            cleaned_role=candidate.role,
            affiliation="TARGET_COMPANY",
            confidence=0.95,
            reason="Unmistakable official first-party leadership evidence",
            source_url=candidate.source_url,
            verification_status="verified",
        )

    if candidate.evidence_type == EvidenceCategory.FIRST_PARTY_EMPLOYEE:
        return CandidateAffiliation(
            name=candidate.name,
            role=candidate.role,
            associated_company=target_domain,
            cleaned_role=candidate.role,
            affiliation="TARGET_COMPANY",
            confidence=0.85,
            reason="Strong first-party employee evidence",
            source_url=candidate.source_url,
            verification_status="verified",
        )

    return CandidateAffiliation(
        name=candidate.name,
        role=candidate.role,
        associated_company=candidate.associated_company,
        cleaned_role=candidate.role,
        affiliation="UNKNOWN",
        confidence=0.3,
        reason="Ambiguous candidate evidence; LinkedIn verification required",
        source_url=candidate.source_url,
        verification_status="ambiguous",
    )

async def test_full_pipeline():
    data = await crawl_domain("supabase.com")
    pages = data.get("pages", {})
    all_candidates = []
    for u, h in pages.items():
        cands = extract_candidate_evidence_refined(u, h, "supabase.com")
        all_candidates.extend(cands)

    # Deduplicate
    unique_candidates_dict = {}
    for cand in all_candidates:
        c_parts = cand.name.split()
        if len(c_parts) < 2:
            continue
        c_key = f"{c_parts[0].lower()} {c_parts[-1].lower()}"

        if c_key not in unique_candidates_dict:
            unique_candidates_dict[c_key] = cand
        else:
            existing = unique_candidates_dict[c_key]
            if cand.verification_status == "verified" and existing.verification_status != "verified":
                unique_candidates_dict[c_key] = cand
            elif cand.verification_status == existing.verification_status:
                has_cand_role = any(r in cand.role.lower() for r in ["ceo", "cto", "cpo", "founder", "president", "vp", "architect", "lead", "director"])
                has_exist_role = any(r in existing.role.lower() for r in ["ceo", "cto", "cpo", "founder", "president", "vp", "architect", "lead", "director"])
                if has_cand_role and not has_exist_role:
                    unique_candidates_dict[c_key] = cand

    affiliations = [evaluate_affiliation_refined(c, "supabase.com") for c in unique_candidates_dict.values()]
    verified = [a for a in affiliations if a.affiliation == "TARGET_COMPANY" and a.confidence >= 0.7]
    rejected = [a for a in affiliations if a.affiliation != "TARGET_COMPANY"]

    print(f"\n================ SUMMARY ================")
    print(f"Total Unique Candidates: {len(unique_candidates_dict)}")
    print(f"Verified Leadership ({len(verified)}):")
    for v in verified:
        print(f"  ★ {v.name} | Role: {v.cleaned_role} | Source: {v.source_url} | Conf: {v.confidence}")
    print(f"Excluded/Rejected ({len(rejected)}):")
    for r in rejected[:10]:
        print(f"  ✖ {r.name} | Role: {r.cleaned_role} | Reason: {r.reason}")

if __name__ == "__main__":
    asyncio.run(test_full_pipeline())
