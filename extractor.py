"""
extractor.py — LLM-Based Structured Data Extraction

Uses the google-genai SDK to extract structured company intelligence
from preprocessed markdown content. Enforces Pydantic schema validation
via Gemini's native JSON output mode with response_schema.
"""

import asyncio
import json
import random
import re
from google import genai
from google.genai import types as genai_types

from config import (
    GEMINI_API_KEY,
    LLM_MODEL,
    GEMINI_MODEL_FALLBACK,
    LLM_TEMPERATURE,
    LLM_MAX_RETRIES,
)
from models import CompanyIntelligence
from search_fallback import metrics
from utils import logger


# ─── Gemini Client Singleton ────────────────────────────────────────────────────

_genai_client: genai.Client | None = None
_selected_model: str | None = None


def _get_genai_client() -> genai.Client:
    """Get or create the google-genai Client singleton."""
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
    return _genai_client


async def validate_and_select_model() -> str:
    """Validate the configured model and fall back if unavailable.

    Tests each model in the fallback list by calling list_models and
    checking that 'generateContent' is supported. Returns the first
    compatible model name.
    """
    global _selected_model
    if _selected_model is not None:
        return _selected_model

    client = _get_genai_client()

    # Build ordered candidate list: configured model first, then fallbacks
    candidates = [LLM_MODEL]
    for m in GEMINI_MODEL_FALLBACK:
        if m not in candidates:
            candidates.append(m)

    logger.info(f"[LLM] Configured model: {LLM_MODEL}")

    # List available models once
    available_models: set[str] = set()
    try:
        models_iter = await client.aio.models.list()
        async for model_info in models_iter:
            available_models.add(model_info.name)  # e.g. "models/gemini-3.5-flash"
    except Exception as e:
        logger.warning(f"[LLM] Could not list models: {e}. Will try configured model directly.")

    for candidate in candidates:
        full_name = f"models/{candidate}"
        if available_models and full_name not in available_models:
            logger.info(f"[LLM] Model {candidate} not in available models list, skipping")
            continue

        # Try a minimal generation to verify the model works
        try:
            test_response = await client.aio.models.generate_content(
                model=candidate,
                contents="Reply with the word OK.",
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=10,
                ),
            )
            if test_response and test_response.text:
                _selected_model = candidate
                logger.info(f"[LLM] ✅ Selected model: {candidate}")
                return candidate
        except Exception as e:
            err_str = str(e)
            if "404" in err_str or "not found" in err_str.lower():
                logger.info(f"[LLM] Model {candidate} unavailable/incompatible, trying next")
            else:
                logger.warning(f"[LLM] Model {candidate} failed validation: {err_str[:100]}")
            continue

    raise RuntimeError(
        f"No compatible Gemini model found. Tried: {candidates}. "
        "Check your GEMINI_API_KEY and available models."
    )


def get_selected_model() -> str:
    """Return the currently selected model name (must call validate_and_select_model first)."""
    if _selected_model is None:
        raise RuntimeError("Model not yet validated. Call validate_and_select_model() first.")
    return _selected_model


# ─── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a precise data extraction agent specializing in B2B company intelligence.
Your task is to analyze preprocessed web content from a company's website and extract structured information.

Guidelines:
- Company Overview: Write exactly 2 clear, informative sentences about what the company does, its core product/service, and value proposition.
- Target Audience / ICP: Describe who the product is built for. Be specific (e.g., "Developers building REST APIs" not just "Developers").
- Contact Points: Only include PUBLIC/GENERIC email addresses (contact@, sales@, support@, press@, etc.). Do NOT include personal emails or fabricated addresses.
- Key Leadership: Only include names and roles of verified employees, founders, or executives of the target company. Do NOT infer employment from a person's presence on the website.
- Data Confidence Score: Rate your confidence based on overall completeness across ALL fields:
  - 0.9-1.0: All fields (Overview, ICP, Emails, Leadership) fully populated with high certainty from clear sources
  - 0.7-0.8: Most fields populated with high certainty, minor gaps
  - 0.5-0.6: Several fields missing or based on limited content
  - 0.3-0.4: Significant gaps, limited useful content found
  - 0.0-0.2: Very little useful information could be extracted

CRITICAL RULES:
- NEVER fabricate data. If information is not found in the sustained evidence, leave the field empty or reflect this in the confidence score.
- Do NOT infer employment from a person's presence on the website. A person appearing anywhere on a company website is not necessarily an employee. Only classify someone as leadership/team when the supplied evidence explicitly associates them with the target company.
- If evidence is ambiguous, return no candidate rather than guessing.
- Do NOT classify individuals from external companies (e.g. Vercel, Netlify, Docker, PagerDuty), investors, or customer testimonials as company leadership.
- Do NOT invent or construct synthetic LinkedIn URLs."""


def _is_external_or_affiliate_role(
    member_name: str,
    member_role: str,
    target_domain: str,
    content: str,
) -> tuple[bool, str]:
    """Determine if an individual's role belongs to an external, parent, subsidiary,
    investor, partner, advisor, or affiliated entity rather than being a current leader of the target company.
    """
    from preprocessor import normalize_org_name, COMMON_ROLE_TITLE_WORDS
    target_norm = normalize_org_name(target_domain)
    role_lower = member_role.lower().strip()
    name_lower = member_name.lower().strip()
    content_lower = content.lower()

    # 1. Non-executive or advisory / partner / customer / investor role indicators
    non_target_role_terms = [
        "advisor", "adviser", "advisory", "board member", "board of directors",
        "investor", "customer", "partner", "podcast guest", "former", "ex-",
        "speaker", "guest", "fellow", "contributor", "affiliate", "subsidiary",
        "parent company",
    ]
    for term in non_target_role_terms:
        if re.search(r'\b' + re.escape(term) + r'\b', role_lower):
            primary_csuite = any(
                c in role_lower for c in ["ceo", "cto", "coo", "cfo", "founder", "cofounder", "president"]
            )
            if not primary_csuite or ("former" in role_lower or "ex-" in role_lower):
                return True, f"role specifies non-target affiliation ('{term}')"

    # 2. Check if role explicitly attaches to an external organization:
    # e.g., "Executive Vice President, CoreAI, Microsoft"
    # e.g., "VP of Engineering at Acme"
    role_segments = [s.strip() for s in re.split(r'[,|–—]', member_role) if s.strip()]
    department_and_title_words = {
        "engineering", "product", "products", "design", "marketing", "sales", "operations",
        "finance", "legal", "people", "security", "ai", "infrastructure", "platform",
        "technology", "technologies", "cloud", "hardware", "software", "revenue",
        "business", "corporate", "global", "strategic", "customer", "success", "growth",
        "coreai", "developer", "enterprise", "consumer", "mobile", "web", "systems",
    }

    if len(role_segments) >= 2:
        last_seg = role_segments[-1]
        last_seg_norm = normalize_org_name(last_seg)
        last_words = [w.lower() for w in last_seg.split()]
        is_title_or_dept = any(w in COMMON_ROLE_TITLE_WORDS or w in department_and_title_words for w in last_words)
        if last_seg_norm and not is_title_or_dept:
            if last_seg_norm != target_norm and last_seg_norm not in ("the company", "our team", "our leadership", "company"):
                return True, f"role explicitly designates external/parent company '{last_seg}'"

    org_in_role_match = re.search(r'\b(?:at|of|for|@)\s+([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+)?)\b', member_role)
    if org_in_role_match:
        org_cand = org_in_role_match.group(1).strip()
        org_cand_norm = normalize_org_name(org_cand)
        cand_words = [w.lower() for w in org_cand.split()]
        if not any(w in COMMON_ROLE_TITLE_WORDS or w in department_and_title_words for w in cand_words):
            if org_cand_norm and org_cand_norm != target_norm and org_cand_norm not in ("the company", "our team"):
                return True, f"role indicates employment at external company '{org_cand}'"

    # 3. Textual evidence tying person directly to an external or parent company as their current role:
    # e.g., "joined Microsoft’s Senior Leadership Team as Executive Vice President"
    if name_lower in content_lower:
        joined_match = re.search(
            rf"\b(?:joined|serves\s+at|is\s+at)\s+([A-Z][A-Za-z0-9]+)(?:'s)?\s+(?:senior\s+leadership|executive|leadership\s+team|as)",
            content,
            re.IGNORECASE
        )
        if joined_match:
            joined_org = normalize_org_name(joined_match.group(1).strip())
            if joined_org and joined_org != target_norm and joined_org not in ("the company", "our team"):
                idx = content_lower.find(name_lower)
                snippet_around = content_lower[max(0, idx - 100):min(len(content_lower), idx + 300)]
                if joined_match.group(0).lower() in snippet_around:
                    return True, f"page states individual joined external entity '{joined_match.group(1)}'"

    return False, ""


async def extract_intelligence(
    domain: str,
    content: str,
    discovered_emails: list[str],
    direct_linkedin_links: list[dict] | None = None,
) -> tuple[CompanyIntelligence, dict]:
    """Extract structured company intelligence from preprocessed content.

    Uses the google-genai SDK with native JSON schema output mode for
    Pydantic-validated structured extraction.

    Args:
        domain: The target domain (for context in the prompt)
        content: Preprocessed markdown content from all crawled pages
        discovered_emails: Emails found during preprocessing
        direct_linkedin_links: Direct LinkedIn profile URLs found in page HTML

    Returns:
        Tuple of (CompanyIntelligence, usage_dict) where usage_dict contains
        token counts from the API response.
    """
    logger.info(f"🤖 Extracting intelligence for {domain} via LLM")

    client = _get_genai_client()
    model_name = get_selected_model()

    user_prompt = f"""Analyze the following preprocessed web content from {domain} and extract structured company intelligence.

The following email addresses were discovered on the website during crawling:
{', '.join(discovered_emails) if discovered_emails else 'No emails discovered during crawling.'}

--- BEGIN WEBSITE CONTENT ---
{content}
--- END WEBSITE CONTENT ---

Extract the company intelligence based on the above content. Remember:
- Only report information explicitly found in the content
- Include the discovered emails in contact_points if they appear legitimate and public
- For key_leadership, assign direct LinkedIn URLs provided in the evidence if they match team members. Do NOT invent/fabricate LinkedIn URLs.
- Set an appropriate data_confidence_score based on the completeness of your findings"""

    combined_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"

    # Build Pydantic schema for structured output
    schema = CompanyIntelligence.model_json_schema()

    response = None
    usage_info = {}

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        logger.info(f"[LLM] {domain} attempt {attempt}/{LLM_MAX_RETRIES}")
        try:
            raw_response = await client.aio.models.generate_content(
                model=model_name,
                contents=combined_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=LLM_TEMPERATURE,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )

            # Parse structured JSON response
            response_text = raw_response.text
            if not response_text:
                raise ValueError("Empty response from Gemini API")

            parsed_data = json.loads(response_text)
            response = CompanyIntelligence.model_validate(parsed_data)

            # Extract usage metadata
            if raw_response.usage_metadata:
                um = raw_response.usage_metadata
                usage_info = {
                    "prompt_tokens": um.prompt_token_count or 0,
                    "completion_tokens": um.candidates_token_count or 0,
                    "total_tokens": um.total_token_count or 0,
                }

            break

        except Exception as exc:
            err_msg = str(exc)

            # Classify the error
            is_rate_limit = any(k in err_msg.lower() for k in ["429", "rate_limit", "too many requests", "resource_exhausted"])
            is_not_found = any(k in err_msg.lower() for k in ["404", "not found", "not_found", "model_decommissioned"])
            is_server_error = any(k in err_msg for k in ["500", "502", "503", "504"])

            if is_not_found:
                logger.error(f"  ❌ [LLM] {domain} model {model_name} not found (404). Not retrying.")
                raise RuntimeError(f"Model {model_name} is not available: {err_msg[:200]}")

            if is_rate_limit:
                if attempt == LLM_MAX_RETRIES:
                    logger.error(f"  ❌ [LLM] {domain} rate limit exhausted after {LLM_MAX_RETRIES} attempts.")
                    raise RuntimeError(f"LLM extraction failed for {domain} due to persistent API rate limits.")

                # Exponential backoff with jitter
                base_delay = 2.0
                wait_time = (base_delay ** attempt) + random.uniform(0.5, 2.0)
                logger.warning(f"  ⚠ [LLM] rate limited; retrying in {wait_time:.1f} seconds")
                await asyncio.sleep(wait_time)
                continue

            if is_server_error and attempt < LLM_MAX_RETRIES:
                wait_time = 3.0 * attempt + random.uniform(0.5, 1.5)
                logger.warning(f"  ⚠ [LLM] server error; retrying in {wait_time:.1f}s: {err_msg[:100]}")
                await asyncio.sleep(wait_time)
                continue

            # Non-retryable error
            logger.error(f"  ❌ [LLM] {domain} failed with error: {err_msg[:200]}")
            raise

    if response is None:
        raise RuntimeError(f"LLM extraction failed for {domain} — no response after {LLM_MAX_RETRIES} attempts.")

    # ─── Post-process: merge emails ─────────────────────────────────────────
    from utils import is_valid_contact_email
    all_emails = [
        e for e in dict.fromkeys(
            response.contact_points + [e for e in discovered_emails if e not in response.contact_points]
        )
        if is_valid_contact_email(e)
    ]
    response.contact_points = all_emails

    # ─── Post-process: enforce strict two-stage leadership verification ──────
    seen_leadership_names = set()
    verified_leadership = []
    excluded_names = set()

    # 1. Parse excluded individuals from preprocessing evidence
    if "EXCLUDED INDIVIDUALS" in content:
        excluded_block = content.split("EXCLUDED INDIVIDUALS")[1].split("================================")[0]
        for line in excluded_block.split("\n"):
            if "- Excluded:" in line:
                try:
                    ex_name = line.split("- Excluded:")[1].split("(")[0].strip()
                    if ex_name:
                        parts = ex_name.split()
                        if len(parts) >= 2:
                            excluded_names.add(f"{parts[0].lower()} {parts[-1].lower()}")
                            excluded_names.add(ex_name.lower())
                except Exception:
                    pass

    # 2. Parse verified first-party leadership from Stage 2 evidence
    if "TARGET COMPANY LEADERSHIP EVIDENCE" in content:
        evidence_block = content.split("TARGET COMPANY LEADERSHIP EVIDENCE")[1].split("================================")[0]
        if "EXCLUDED INDIVIDUALS" in evidence_block:
            evidence_block = evidence_block.split("EXCLUDED INDIVIDUALS")[0]

        for line in evidence_block.split("\n"):
            if line.startswith("- Name:"):
                try:
                    parts = line.split("|")
                    c_name = parts[0].replace("- Name:", "").strip()
                    c_role = parts[1].replace("Role:", "").strip() if len(parts) > 1 else "Key Team Member"

                    c_source = None
                    if "(Source:" in line:
                        c_source = line.split("(Source:")[1].split(")")[0].strip()

                    if "[LinkedIn:" in c_role:
                        c_role = c_role.split("[LinkedIn:")[0].strip()

                    import re
                    for stop_phrase in ["trusted at", "unified platform", "source:", "(source:"]:
                        if stop_phrase in c_role.lower():
                            c_role = re.split(re.escape(stop_phrase), c_role, flags=re.IGNORECASE)[0].strip()
                    if "(" in c_role:
                        c_role = c_role.split("(")[0].strip()
                    c_role = c_role.rstrip(" (,").strip()

                    c_linkedin = None
                    if "[LinkedIn:" in line:
                        c_linkedin = line.split("[LinkedIn:")[1].split("]")[0].strip()

                    # Require explicit leadership/team title or verified LinkedIn URL
                    if c_role.lower() in ["key team member", "team member", "author"] and not c_linkedin:
                        continue

                    name_parts = c_name.split()
                    if len(name_parts) >= 2:
                        canonical_key = f"{name_parts[0].lower()} {name_parts[-1].lower()}"
                        # Ensure not in excluded list
                        if canonical_key not in excluded_names and c_name.lower() not in excluded_names:
                            from models import TeamMember
                            verified_leadership.append(TeamMember(
                                name=c_name,
                                role=c_role,
                                linkedin_url=c_linkedin,
                                source_url=c_source,
                                linkedin_source="website" if c_linkedin else None,
                            ))
                            seen_leadership_names.add(canonical_key)
                except Exception:
                    pass

    # 3. Filter LLM key_leadership (The LLM must NOT directly add unverified individuals)
    from preprocessor import (
        NON_PERSON_WORDS,
        COMMON_ROLE_TITLE_WORDS,
        is_plausible_person_name,
        disambiguate_name_and_company,
        normalize_org_name,
    )
    target_norm = normalize_org_name(domain)

    for member in response.key_leadership:
        mname = member.name.strip()
        if mname.lower().startswith("follow "):
            mname = mname[7:].strip()

        # Disambiguate name and any attached organization generically
        mname, assoc_org = disambiguate_name_and_company(mname, content)

        if not is_plausible_person_name(mname, target_norm):
            continue

        mparts = mname.split()
        canonical_key = f"{mparts[0].lower()} {mparts[-1].lower()}"

        # Reject anyone in excluded list or associated with external organizations
        if canonical_key in excluded_names or mname.lower() in excluded_names:
            logger.info(f"  🚫 Excluding LLM-proposed candidate '{mname}' (identified as external entity)")
            metrics.candidates_found += 1
            metrics.candidates_rejected += 1
            continue

        if assoc_org:
            norm_assoc = normalize_org_name(assoc_org)
            if norm_assoc and norm_assoc != target_norm:
                logger.info(f"  🚫 Excluding LLM candidate with external company '{assoc_org}': '{mname}'")
                metrics.candidates_found += 1
                metrics.candidates_rejected += 1
                continue

        # Reject non-target / parent / affiliate / customer / partner / advisor roles
        is_ext, ext_reason = _is_external_or_affiliate_role(mname, member.role, domain, content)
        if is_ext:
            logger.info(f"  🚫 Excluding non-target/affiliate candidate '{mname}' ({member.role}): {ext_reason}")
            metrics.candidates_found += 1
            metrics.candidates_rejected += 1
            continue

        # If already in verified_leadership, update role/linkedin/source if richer
        if canonical_key in seen_leadership_names:
            for v in verified_leadership:
                vparts = v.name.split()
                vkey = f"{vparts[0].lower()} {vparts[-1].lower()}"
                if vkey == canonical_key:
                    if not v.linkedin_url and member.linkedin_url:
                        v.linkedin_url = member.linkedin_url
                        v.linkedin_source = member.linkedin_source or "website"
                    if not v.source_url and member.source_url:
                        v.source_url = member.source_url
        else:
            # ─── Generalized First-Party Leadership Verification ─────────────────
            # Expands beyond narrow CEO/CTO patterns to recognize credible senior
            # leadership (C-suite, President, VP-level, Head of, Director) when
            # supported by unmistakable first-party evidence tying them to the company.
            target_norm = normalize_org_name(domain)
            role_lower = member.role.lower()
            name_lower = mname.lower()
            content_lower = content.lower()

            _LEADERSHIP_ROLE_TOKENS = [
                "ceo", "cto", "coo", "cfo", "cpo", "cro", "cmo", "ciso", "cio", "cbo",
                "chief executive", "chief technology", "chief operating", "chief financial",
                "chief product", "chief revenue", "chief marketing", "chief security",
                "chief business", "chief legal", "chief information", "chief people",
                "general counsel", "founder", "co-founder", "cofounder", "president",
                "executive vice president", "senior vice president", "vice president",
                "evp", "svp", "vp", "head of", "managing director", "executive director",
            ]

            import re
            def _wb_check(kw: str, text: str) -> bool:
                return bool(re.search(r'\b' + re.escape(kw) + r'\b', text))

            has_leadership_role = any(
                r == role_lower or (len(r) <= 3 and _wb_check(r, role_lower)) or (len(r) > 3 and r in role_lower)
                for r in _LEADERSHIP_ROLE_TOKENS
            )

            # Check if text describes person as executive at an external organization
            ext_match = re.search(rf"\b{re.escape(name_lower)}\b[^,\n\.\;]{{0,60}}\b(?:at|of)\s+([A-Z][a-zA-Z0-9\s]+)", content)
            if ext_match:
                found_org = normalize_org_name(ext_match.group(1).strip())
                if found_org and found_org != target_norm and found_org not in ("the company", "the team", "our team", "our leadership"):
                    logger.info(f"  🚫 Excluding candidate with external organization '{found_org}': '{mname}'")
                    metrics.candidates_found += 1
                    metrics.candidates_rejected += 1
                    continue

            # Multi-representation name check:
            # Supports exact full name or structured multi-representation (first name in role bio,
            # surname in headings/bylines/metadata on the same first-party page).
            first_name = mparts[0].lower() if mparts else ""
            last_name = mparts[-1].lower() if len(mparts) > 1 else ""

            exact_name_present = name_lower in content_lower
            split_name_present = (
                not exact_name_present
                and len(mparts) >= 2
                and first_name in content_lower
                and last_name in content_lower
                and (
                    f"{first_name} is " in content_lower
                    or f"{first_name} leads" in content_lower
                    or f"{first_name}, " in content_lower
                    or f"{first_name} serves as" in content_lower
                )
            )
            has_name_evidence = exact_name_present or split_name_present

            # Association with target company
            company_tied = (
                f"{target_norm}'s" in content_lower
                or f"at {target_norm}" in content_lower
                or f"our {role_lower}" in content_lower
                or f"our team" in content_lower
                or f"our leadership" in content_lower
                or "leadership" in content_lower
                or "executive" in content_lower
                or "board of directors" in content_lower
            )

            # Association with leadership role in content
            role_keywords = [
                w for w in role_lower.replace("&", " ").replace("/", " ").replace("-", " ").split()
                if len(w) > 2 and w not in ("and", "the", "for", "with")
            ]
            role_tied = (
                role_lower in content_lower
                or (len(role_keywords) >= 1 and all(rw in content_lower for rw in role_keywords[:2]))
                or f"{name_lower}," in content_lower
                or f"{first_name} is" in content_lower
            )

            is_unmistakable_leader = (
                has_leadership_role
                and has_name_evidence
                and company_tied
                and role_tied
            )

            if is_unmistakable_leader and len(mparts) in [2, 3]:
                mrole = member.role.strip()
                for stop_phrase in ["trusted at", "unified platform", "source:", "(source:"]:
                    if stop_phrase in mrole.lower():
                        mrole = re.split(re.escape(stop_phrase), mrole, flags=re.IGNORECASE)[0].strip()
                if "(" in mrole:
                    mrole = mrole.split("(")[0].strip()
                mrole = mrole.rstrip(" (,").strip()

                member.name = mname
                member.role = mrole
                verified_leadership.append(member)
                seen_leadership_names.add(canonical_key)
                metrics.candidates_found += 1
                metrics.candidates_verified += 1
            else:
                reasons = []
                if not has_leadership_role:
                    reasons.append(f"role '{member.role}' not recognized as senior leadership")
                if not has_name_evidence:
                    reasons.append("name not substantiated by first-party page evidence")
                if not company_tied:
                    reasons.append("not tied to target company in first-party text")
                if not role_tied:
                    reasons.append("role not associated with person in text")
                if len(mparts) not in [2, 3]:
                    reasons.append(f"name token count {len(mparts)} outside allowed [2, 3]")
                reason_str = "; ".join(reasons) or "insufficient first-party evidence"
                logger.info(f"  🚫 Excluding unverified candidate '{mname}' ({member.role}): {reason_str}")
                metrics.candidates_found += 1
                metrics.candidates_rejected += 1

    response.key_leadership = verified_leadership


    # ─── Recalculate holistic confidence ────────────────────────────────────
    has_overview = len(response.company_overview) > 30
    has_icp = len(response.target_audience) > 20
    has_emails = len(response.contact_points) > 0
    has_leadership = len(response.key_leadership) > 0

    if has_overview and has_icp and has_emails and has_leadership:
        response.data_confidence_score = 0.90
    elif has_overview and has_icp and (has_emails or has_leadership):
        response.data_confidence_score = 0.80
    elif has_overview and has_icp:
        response.data_confidence_score = 0.70
    else:
        response.data_confidence_score = 0.50

    logger.info(
        f"  ✅ Extraction complete for {domain} "
        f"(confidence: {response.data_confidence_score:.2f})"
    )
    if usage_info:
        logger.info(
            f"  📊 Tokens — prompt: {usage_info.get('prompt_tokens', '?')}, "
            f"completion: {usage_info.get('completion_tokens', '?')}, "
            f"total: {usage_info.get('total_tokens', '?')}"
        )

    return response, usage_info
