import pytest
from pydantic import ValidationError
from models import (
    TeamMember,
    CompanyIntelligence,
    CrawlStats,
    DomainResult,
    PipelineOutput,
)

def test_team_member_schema():
    member = TeamMember(
        name="Abhinav Asthana",
        role="CEO & Co-founder",
        linkedin_url="https://www.linkedin.com/in/abhinavasthana",
        source_url="https://postman.com/company/about-postman/",
        linkedin_source="website"
    )
    assert member.name == "Abhinav Asthana"
    assert member.role == "CEO & Co-founder"
    assert member.linkedin_url == "https://www.linkedin.com/in/abhinavasthana"
    assert member.source_url == "https://postman.com/company/about-postman/"
    assert member.linkedin_source == "website"

def test_company_intelligence_validation():
    intel = CompanyIntelligence(
        company_overview="Postman is an API platform. It streamlines software development.",
        target_audience="Developers and API teams",
        contact_points=["contact@postman.com"],
        key_leadership=[
            TeamMember(name="Abhinav Asthana", role="CEO & Co-founder")
        ],
        data_confidence_score=0.90
    )
    assert intel.data_confidence_score == 0.90
    assert len(intel.contact_points) == 1
    assert len(intel.key_leadership) == 1

def test_confidence_score_bounds():
    with pytest.raises(ValidationError):
        CompanyIntelligence(
            company_overview="Overview",
            target_audience="Audience",
            contact_points=[],
            key_leadership=[],
            data_confidence_score=1.5  # Invalid: > 1.0
        )
