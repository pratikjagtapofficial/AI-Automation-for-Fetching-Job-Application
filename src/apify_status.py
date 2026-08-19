"""Turn an Apify HTTP error into a message that says what actually broke.

Confirmed live 2026-08-19: Naukri and careerpages both went to zero
postings on 2026-08-16 and stayed there. Both are Apify-backed, and both
were reporting only "HTTP 403" -- technically accurate, but it reads like
a transient network blip in a list of 26 source problems, so three days
of scans delivered nothing and nothing said why. The real cause was the
$5/month Apify free credit being exhausted ("Monthly usage hard limit
exceeded"), which does not resolve on its own and does not resolve by
retrying: it needs the cycle to roll over, or a payment method.

A source that is dead until a date must not look like a source that
happened to find nothing.
"""

from __future__ import annotations

import httpx


def explain(r: httpx.Response, source: str) -> str:
    """-> a one-line error string for the run summary."""
    body = ""
    try:
        body = ((r.json() or {}).get("error") or {}).get("message", "")
    except Exception:
        pass

    low = body.lower()
    if r.status_code == 403 and ("usage hard limit" in low or "limit exceeded" in low):
        return (f"{source}: 🛑 APIFY FREE CREDIT EXHAUSTED — this source is "
                f"DEAD until the monthly cycle resets. Not a glitch, retrying "
                f"will not fix it.")
    if r.status_code in (401, 403) and "token" in low:
        return f"{source}: Apify token rejected ({body[:80]})"
    if body:
        return f"{source}: HTTP {r.status_code} — {body[:100]}"
    return f"{source}: HTTP {r.status_code}"


def is_quota_error(err: str) -> bool:
    """True for the credit-exhausted message above. Lets the run summary
    promote this above ordinary per-source noise."""
    return "APIFY FREE CREDIT EXHAUSTED" in err
