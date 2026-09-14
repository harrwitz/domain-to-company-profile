"""
models.py — Pydantic Schemas for Structured LLM Output

Defines strict data models that enforce the shape of extracted intelligence.
Instructor uses these schemas to validate LLM responses and auto-retry on failure.
"""

from pydantic import BaseModel, Field
from typing import Optional


class TeamMember(BaseModel):
    """A key leadership or team member discovered from the website."""

    name: str = Field(
        ...,
        description="Full name of the team member",
    )
    role: str = Field(
        ...,
        description="Job title or role (e.g., 'CEO', 'CTO', 'VP of Engineering')",
    )
    linkedin_url: Optional[str] = Field(
        default=None,
        description="LinkedIn profile URL if found on the page (e.g., 'https://linkedin.com/in/username')",
    )
    source_url: Optional[str] = Field(
        default=None,
        description="Source page URL where evidence for this member was discovered",
    )
    linkedin_source: Optional[str] = Field(
        default=None,
        description="Origin of the LinkedIn URL ('website', 'search', or None)",
    )


class LeadershipCandidate(BaseModel):
    """Internal candidate representation with rich evidence context (Rule 2)."""

    name: str = Field(..., description="Full candidate name (pure person name, no company name)")
    role: str = Field(default="Key Team Member", description="Extracted role or title")
    associated_company: Optional[str] = Field(default=None, description="Company explicitly associated with person in text")
    source_url: str = Field(default="", description="Exact page URL where candidate was discovered")
    surrounding_text: str = Field(default="", description="Snippet of text surrounding candidate name")
    evidence_type: str = Field(default="unknown", description="Type: 'strong_first_party', 'weak', or 'external'")
    employment_evidence: str = Field(default="", description="Detailed summary of positive and negative evidence")
    linkedin_url: Optional[str] = Field(default=None, description="Discovered or verified LinkedIn profile URL")
    linkedin_current_company: Optional[str] = Field(default=None, description="Current company verified from LinkedIn profile")
    verification_status: str = Field(default="unverified", description="'verified', 'rejected', 'ambiguous', or 'unverified'")
    page_type: str = Field(default="general", description="Page type (e.g. 'team', 'about', 'customer', 'blog')")
    section_heading: str = Field(default="", description="Nearest true section heading element")
    organization_mentions: list[str] = Field(default_factory=list, description="Organization names mentioned in context")
    positive_signals: list[str] = Field(default_factory=list, description="Evidence signals supporting target company employment")
    negative_signals: list[str] = Field(default_factory=list, description="Evidence signals indicating external affiliation/testimonial/customer")
    evidence_score: float = Field(default=0.0, description="Numerical evidence score")

    @property
    def page_url(self) -> str:
        """Alias for backward compatibility."""
        return self.source_url

    def __init__(self, **data):
        # Support page_url as alias for source_url during initialization
        if "page_url" in data and "source_url" not in data:
            data["source_url"] = data.pop("page_url")
        super().__init__(**data)


class CandidateAffiliation(BaseModel):
    """Affiliation decision returned by evidence classification."""

    name: str
    role: str
    associated_company: Optional[str] = None
    affiliation: str = Field(..., description="'TARGET_COMPANY', 'OTHER_COMPANY', or 'UNKNOWN'")
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., description="Explanation of affiliation decision")
    cleaned_role: str = Field(..., description="Role containing ONLY title (no company names or 'customer')")
    source_url: str = Field(..., description="Page URL where evidence was found")
    linkedin_url: Optional[str] = None
    verification_status: str = Field(default="unverified", description="'verified', 'rejected', or 'ambiguous'")
    evidence_score: float = Field(default=0.0, description="Numerical evidence score")


class CompanyIntelligence(BaseModel):
    """Structured intelligence extracted from a company's public web presence."""

    company_overview: str = Field(
        ...,
        description=(
            "A concise 2-sentence summary of what the company does, "
            "its core product/service, and value proposition."
        ),
    )
    target_audience: str = Field(
        ...,
        description=(
            "Description of the company's target audience or Ideal Customer Profile (ICP). "
            "E.g., 'Developers building backend applications' or "
            "'Enterprise teams managing API workflows'."
        ),
    )
    contact_points: list[str] = Field(
        default_factory=list,
        description=(
            "List of generic or public email addresses found on the site "
            "(e.g., 'contact@company.com', 'sales@company.com', 'support@company.com'). "
            "Do NOT include personal emails."
        ),
    )
    key_leadership: list[TeamMember] = Field(
        default_factory=list,
        description=(
            "Key leadership or team members with their names, roles/titles, "
            "and LinkedIn profile URLs if discoverable from the page content."
        ),
    )
    data_confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Estimated confidence score between 0.0 and 1.0 indicating the "
            "quality and completeness of the extracted data. "
            "1.0 = all fields fully populated with high certainty. "
            "0.5 = some fields missing or uncertain. "
            "0.0 = very low quality or almost no useful data found."
        ),
    )


class SelectedPageInfo(BaseModel):
    """Metadata and relevance details for a page selected for crawling."""

    url: str = Field(..., description="The resolved URL of the selected page")
    score: int = Field(default=0, description="Computed relevance score for the link")
    reason: str = Field(default="", description="Explanation of why this page was selected")
    title: Optional[str] = Field(default=None, description="Page title tag content if available")
    is_leadership_relevant: bool = Field(default=False, description="Whether page matched leadership/team signals")


class CrawlStats(BaseModel):
    """Crawl diagnostics for a domain."""

    pages_crawled: int = Field(default=0, description="Number of pages successfully crawled")
    pages_failed: int = Field(default=0, description="Number of candidate pages that failed to load")
    candidate_links_found: int = Field(default=0, description="Total internal candidate links discovered")
    relevant_links_selected: int = Field(default=0, description="Number of top relevant links selected for crawling")
    leadership_pages_found: int = Field(default=0, description="Number of leadership-relevant candidate links discovered")
    selected_pages: list[SelectedPageInfo] = Field(default_factory=list, description="Metadata and scores for selected pages")
    candidates_detected: int = Field(default=0, description="Number of leadership candidates detected during preprocessing")
    candidates_verified: int = Field(default=0, description="Number of candidates verified as target company leaders")
    candidates_rejected: int = Field(default=0, description="Number of candidates rejected due to external affiliation or insufficient evidence")



class DomainResult(BaseModel):
    """Complete result for a single domain, including metadata."""

    domain: str = Field(..., description="The target domain that was analyzed")
    intelligence: CompanyIntelligence = Field(
        ...,
        description="Extracted structured intelligence",
    )
    pages_crawled: int = Field(
        default=0,
        description="Number of pages successfully crawled",
    )
    crawl_stats: Optional[CrawlStats] = Field(
        default=None,
        description="Detailed crawl statistics for this domain",
    )
    model_used: Optional[str] = Field(
        default=None,
        description="LLM model used for intelligence extraction",
    )
    cost_usd: Optional[float] = Field(
        default=None,
        description="Estimated API cost in USD for this domain",
    )
    tokens_used: Optional[int] = Field(
        default=None,
        description="Total tokens consumed for this domain",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Any errors or warnings encountered during processing",
    )


class PipelineOutput(BaseModel):
    """Top-level output schema for the entire pipeline run."""

    results: list[DomainResult] = Field(
        default_factory=list,
        description="Results for each processed domain",
    )
    total_domains: int = Field(default=0, description="Total domains processed")
    model_used: Optional[str] = Field(
        default=None,
        description="LLM model used for the pipeline run",
    )
    total_cost_usd: float = Field(
        default=0.0,
        description="Total estimated API cost in USD",
    )
    total_tokens: int = Field(
        default=0,
        description="Total tokens consumed across all domains",
    )
