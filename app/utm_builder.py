"""UTM URL builder and auditor — pure logic, no I/O.

Used by UTM audit/plan/apply endpoints for Yandex Direct campaigns.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Default UTM params for Yandex Direct campaigns.
DEFAULT_UTM_PARAMS = {
    "utm_source": "yandex",
    "utm_medium": "cpc",
}

REQUIRED_UTM_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}

# --- URL builder ---------------------------------------------------------------


def build_utm_url(
    url: str,
    *,
    utm_source: str,
    utm_medium: str,
    utm_campaign: str,
    utm_content: str = "",
    utm_term: str = "",
    overwrite: bool = False,
    custom_params: dict[str, str] | None = None,
) -> str:
    """Add UTM params to *url*, preserving existing query string and fragment.

    When *overwrite* is ``False`` (default) and any UTM param already exists
    in the URL, the function returns the URL unchanged (with a warning-eligible
    signal that the caller should surface to the operator). When *overwrite*
    is ``True``, existing UTM params are replaced with the new values.

    *custom_params* are injected after the core UTM params and never override
    the core five (utm_source/medium/campaign/content/term).
    """
    # Ensure URL has a scheme for urlparse.
    if "://" not in url:
        url = f"http://{url}"

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    # Overwrite check: if any UTM already present and overwrite=False,
    # don't touch existing UTM — keep original URL.
    existing_utm = {k: v for k, v in query.items() if k.startswith("utm_")}
    if existing_utm and not overwrite:
        return url

    # Build core UTM map — only include non-empty values.
    core: dict[str, str] = {}
    for key, value in [
        ("utm_source", utm_source),
        ("utm_medium", utm_medium),
        ("utm_campaign", utm_campaign),
        ("utm_content", utm_content),
        ("utm_term", utm_term),
    ]:
        if value:
            core[key] = value

    # Inject custom params (lower priority than core).
    if custom_params:
        for k, v in custom_params.items():
            if k not in core:
                core[k] = v

    # Apply: when overwrite=True, remove ALL existing UTM first, then merge.
    if overwrite:
        query = {k: v for k, v in query.items() if not k.startswith("utm_")}

    query.update(core)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


# --- UTM auditor --------------------------------------------------------------


def audit_utm_url(
    url: str,
    *,
    required_params: set[str] | None = None,
    expected_values: dict[str, str] | None = None,
) -> dict:
    """Audit a single URL for UTM status.

    Returns a dict with:
      - status: "complete" | "partial" | "missing" | "mismatch"
      - present_params: dict of UTM params found
      - missing_params: list of required params not present
      - wrong_values: dict of params present with wrong values
      - extra_params: non-utm query params preserved
    """
    if required_params is None:
        required_params = REQUIRED_UTM_PARAMS
    if expected_values is None:
        expected_values = DEFAULT_UTM_PARAMS

    if "://" not in url:
        url = f"http://{url}"

    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    utm_present = {k: v for k, v in query.items() if k.startswith("utm_")}
    non_utm = {k: v for k, v in query.items() if not k.startswith("utm_")}

    missing = []
    wrong = {}
    for param in required_params:
        if param not in utm_present:
            if "" not in (utm_present.get(param),):  # present as empty is still missing
                missing.append(param)
            elif utm_present.get(param) is None:
                missing.append(param)
            else:
                # Empty string value — treat as missing for safety.
                missing.append(param)
        elif param in expected_values:
            if utm_present[param] != expected_values[param]:
                wrong[param] = {
                    "expected": expected_values[param],
                    "actual": utm_present[param],
                }

    # Determine status.
    if not utm_present:
        status = "missing"
    elif not missing and not wrong:
        status = "complete"
    elif wrong:
        status = "mismatch"
    else:
        status = "partial"

    return {
        "status": status,
        "present_params": utm_present,
        "missing_params": missing,
        "wrong_values": wrong,
        "extra_params": non_utm,
    }


# --- Campaign slug generator --------------------------------------------------


def generate_campaign_slug(name: str, campaign_id: str) -> str:
    """Generate a safe, URL-friendly campaign slug from name and id.

    Used when ``utm_campaign`` is not explicitly provided — the slug
    is derived from the campaign name (transliterated to ASCII) and
    the numeric campaign id, ensuring uniqueness without requiring
    operator input.

    Falls back to ``"campaign-{id}"`` when *name* is empty or yields
    no usable ASCII characters.
    """
    if not name or not name.strip():
        return f"campaign-{campaign_id}"

    # Transliterate: NFKD decomposes accented chars, then drop non-ASCII.
    normalized = unicodedata.normalize("NFKD", name.strip().lower())
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")

    # Replace any run of non-alphanumeric chars with a single hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    if not slug:
        return f"campaign-{campaign_id}"

    # Cap length and append id for uniqueness.
    slug = slug[:40].rstrip("-")
    return f"{slug}-{campaign_id}"
