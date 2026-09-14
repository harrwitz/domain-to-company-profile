import pytest
from models import CompanyIntelligence, TeamMember

def test_deduplicate_leadership_candidates():
    members = [
        TeamMember(name="Abhinav Asthana", role="CEO & Co-founder", linkedin_url="https://linkedin.com/in/abhinavasthana"),
        TeamMember(name="Abhinav", role="Key Team Member", linkedin_url="https://linkedin.com/in/abhinavasthana"),
        TeamMember(name="Ankit Sobti", role="CTO & Co-founder", linkedin_url="https://linkedin.com/in/ankitsobti"),
    ]
    
    # Deduplication logic simulation
    seen = set()
    cleaned = []
    for m in members:
        parts = m.name.split()
        if len(parts) >= 2:
            key = f"{parts[0].lower()} {parts[-1].lower()}"
            if key not in seen:
                seen.add(key)
                cleaned.append(m)

    assert len(cleaned) == 2
    names = [m.name for m in cleaned]
    assert "Abhinav Asthana" in names
    assert "Ankit Sobti" in names
    assert "Abhinav" not in names
