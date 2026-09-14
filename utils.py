"""
utils.py — Shared Utilities

Logging setup, URL normalization, and common helper functions.
"""

import re
import logging
import sys
from urllib.parse import urljoin, urlparse
from rich.console import Console
from rich.logging import RichHandler

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(highlight=False)


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure structured logging with Rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
            )
        ],
    )
    logger = logging.getLogger("lead_enrichment")
    logger.setLevel(level)
    return logger


logger = setup_logging()


def normalize_domain(domain: str) -> str:
    """Normalize a domain string to a clean host domain.

    Examples:
        'https://postman.com/' -> 'postman.com'
        'https://www.postman.com/about/' -> 'postman.com'
        'SUPABASE.COM' -> 'supabase.com'
    """
    domain = domain.strip().lower()
    if "://" not in domain:
        domain = "https://" + domain
    parsed = urlparse(domain)
    netloc = parsed.netloc or parsed.path.split("/")[0]
    netloc = re.sub(r"^www\.", "", netloc)
    return netloc.split(":")[0]


def domain_to_url(domain: str) -> str:
    """Convert a domain to a full HTTPS URL.

    Examples:
        'postman.com' -> 'https://postman.com'
    """
    domain = normalize_domain(domain)
    return f"https://{domain}"


def is_same_domain(url: str, domain: str) -> bool:
    """Check if a URL belongs to the same domain (including subdomains)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    domain = domain.lower()
    return host == domain or host.endswith(f".{domain}")


def resolve_url(base_url: str, href: str) -> str | None:
    """Resolve a relative or absolute href against a base URL.

    Returns None for non-HTTP URLs (mailto, tel, javascript, etc.).
    """
    if not href:
        return None

    # Skip non-HTTP schemes
    skip_prefixes = ("mailto:", "tel:", "javascript:", "#", "data:")
    if any(href.lower().startswith(p) for p in skip_prefixes):
        return None

    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)

    # Only keep HTTP(S) URLs
    if parsed.scheme not in ("http", "https"):
        return None

    # Remove fragments
    resolved = parsed._replace(fragment="").geturl()
    return resolved


RESERVED_AND_EXAMPLE_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "domain.com", "yourdomain.com", "company.com", "yourcompany.com",
    "mycompany.com", "site.com", "website.com", "sample.com",
    "test.com", "invalid.com", "placeholder.com"
}

RESERVED_TLDS = {
    ".example", ".invalid", ".localhost", ".test", ".local"
}

PLACEHOLDER_LOCAL_PARTS = {
    "you", "your-name", "yourname", "username", "user", "test", "testing",
    "name", "first.last", "firstname.lastname", "someone", "placeholder",
    "email", "sample", "demo"
}

EXCLUDED_EMAIL_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".js", ".mjs", ".css", ".json", ".map", ".ts", ".tsx", ".vue"
}


def is_valid_contact_email(email: str) -> bool:
    """Validate that an email address is syntactically valid and not a template placeholder or example address.

    Rejects:
      - obvious placeholder addresses (you@domain.com, user@example.com, test@example.com, name@example.com)
      - reserved and example domains (example.com, domain.com, .invalid, .test, .localhost)
      - file extension false positives (.png, .js, etc.)
      - malformed syntax
    Retains:
      - legitimate company public addresses (info@, support@, sales@, security@, legal@, etc.)
      - verified employee/personal company addresses (jane.diaz@stripe.com, etc.)
    """
    if not email or not isinstance(email, str):
        return False

    email = email.strip().lower()

    # Clean HTML entity artifacts like u003e, gt, lt
    email = re.sub(r"^(?:u003e|u003c|gt|lt|amp)+", "", email)

    if "@" not in email:
        return False

    parts = email.split("@")
    if len(parts) != 2:
        return False

    local_part, domain_part = parts[0], parts[1]

    if not local_part or not domain_part:
        return False

    if any(email.endswith(ext) for ext in EXCLUDED_EMAIL_EXTENSIONS):
        return False

    if "." not in domain_part:
        return False

    tld = "." + domain_part.split(".")[-1]
    if len(tld) < 3 or tld in RESERVED_TLDS:
        return False

    # Check reserved and example domains
    if (
        domain_part in RESERVED_AND_EXAMPLE_DOMAINS
        or domain_part.endswith(".example.com")
        or domain_part.endswith(".example.org")
        or domain_part.endswith(".domain.com")
    ):
        return False

    # Check placeholder local parts paired with generic or template domains
    if local_part in PLACEHOLDER_LOCAL_PARTS:
        if (
            domain_part in RESERVED_AND_EXAMPLE_DOMAINS
            or local_part in {"you", "yourname", "first.last", "firstname.lastname", "placeholder"}
            or domain_part in {"domain.com", "example.com", "test.com", "sample.com", "email.com"}
        ):
            return False

    # Local part cannot start or end with punctuation
    if not local_part[0].isalnum() or not local_part[-1].isalnum():
        return False

    # Standard regex check for full email
    email_regex = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if not re.match(email_regex, email):
        return False

    return True


def extract_emails_from_text(text: str) -> list[str]:
    """Extract email addresses from text using regex, filtered with is_valid_contact_email."""
    if not text or not isinstance(text, str):
        return []

    pattern = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    raw_emails = re.findall(pattern, text)

    filtered: list[str] = []
    seen: set[str] = set()

    for email in raw_emails:
        email_clean = email.strip().lower()
        email_clean = re.sub(r"^(?:u003e|u003c|gt|lt|amp)+", "", email_clean)
        if email_clean in seen:
            continue
        if is_valid_contact_email(email_clean):
            seen.add(email_clean)
            filtered.append(email_clean)

    return filtered


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate text to a maximum character count, breaking at word boundaries."""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    # Try to break at the last space
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.8:
        truncated = truncated[:last_space]

    return truncated + "\n\n[Content truncated for token optimization]"
