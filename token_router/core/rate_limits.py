"""Provider Rate-Limit Intelligence & Response Header Extraction.

Safely extracts and canonicalizes rate-limit headers across AI providers:
- OpenAI (x-ratelimit-remaining-requests, x-ratelimit-remaining-tokens, x-ratelimit-reset-requests, etc.)
- Anthropic (anthropic-ratelimit-requests-remaining, anthropic-ratelimit-tokens-remaining, retry-after)
- Google Gemini (retry-after, x-ratelimit-remaining, error details 429)
- OpenRouter (x-ratelimit-remaining, retry-after)
- Standard RFC 7231 Retry-After (seconds or HTTP date)

Treats missing, ambiguous, or malformed headers safely.
"""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional
from pydantic import BaseModel, Field


class ParsedRateLimit(BaseModel):
    """Normalized rate limit metrics parsed from upstream provider response headers."""
    provider: str
    remaining_requests: Optional[int] = None
    limit_requests: Optional[int] = None
    remaining_tokens: Optional[int] = None
    limit_tokens: Optional[int] = None
    reset_requests_seconds: Optional[float] = None
    reset_tokens_seconds: Optional[float] = None
    retry_after_seconds: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_quota_info(self) -> bool:
        return any(
            v is not None
            for v in (
                self.remaining_requests,
                self.remaining_tokens,
                self.retry_after_seconds,
            )
        )


def _safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        # Handles strings like "1000", "1000.0", or numbers
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def _parse_duration(val: Any) -> Optional[float]:
    """Parses duration strings like '6m0s', '15ms', '20s', '1.5' into float seconds."""
    if val is None:
        return None
    s = str(val).strip().lower()
    try:
        return float(s)
    except ValueError:
        pass

    # Pattern for combinations like "6m0s", "10s", "500ms"
    total_sec = 0.0
    matched = False
    m_match = re.search(r"(\d+(?:\.\d+)?)\s*m(?:in)?(?![a-z])", s)
    if m_match:
        total_sec += float(m_match.group(1)) * 60.0
        matched = True
    s_match = re.search(r"(\d+(?:\.\d+)?)\s*s(?:ec)?(?![a-z])", s)
    if s_match:
        total_sec += float(s_match.group(1))
        matched = True
    ms_match = re.search(r"(\d+(?:\.\d+)?)\s*ms", s)
    if ms_match:
        total_sec += float(ms_match.group(1)) / 1000.0
        matched = True

    return total_sec if matched else None


def _parse_retry_after(val: Any) -> Optional[float]:
    """Parses standard HTTP Retry-After header (either integer seconds or HTTP-date)."""
    if val is None:
        return None
    s = str(val).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass

    try:
        dt = parsedate_to_datetime(s)
        now = datetime.now(UTC)
        diff = (dt - now).total_seconds()
        return max(0.0, diff)
    except Exception:
        return None


class RateLimitExtractor:
    """Extracts rate limit and quota information from HTTP response headers."""

    @classmethod
    def extract(
        cls,
        provider: str,
        headers: Mapping[str, str],
        status_code: int = 200,
        response_body: Optional[Any] = None,
    ) -> ParsedRateLimit:
        p_lower = provider.lower()
        # Case-insensitive header dictionary
        h = {k.lower(): str(v).strip() for k, v in headers.items()}

        retry_after = _parse_retry_after(h.get("retry-after"))
        if retry_after is None:
            retry_after = _parse_retry_after(h.get("retry-after-ms"))
            if retry_after is not None:
                retry_after = retry_after / 1000.0

        if p_lower == "openai":
            return cls._extract_openai(h, retry_after)
        elif p_lower == "anthropic":
            return cls._extract_anthropic(h, retry_after)
        elif p_lower == "gemini":
            return cls._extract_gemini(h, retry_after, status_code, response_body)
        elif p_lower == "openrouter":
            return cls._extract_openrouter(h, retry_after)
        else:
            # Generic extractor based on standard headers
            return cls._extract_generic(p_lower, h, retry_after)

    @classmethod
    def _extract_openai(cls, h: Dict[str, str], retry_after: Optional[float]) -> ParsedRateLimit:
        rem_req = _safe_int(h.get("x-ratelimit-remaining-requests"))
        limit_req = _safe_int(h.get("x-ratelimit-limit-requests"))
        rem_tok = _safe_int(h.get("x-ratelimit-remaining-tokens"))
        limit_tok = _safe_int(h.get("x-ratelimit-limit-tokens"))
        reset_req = _parse_duration(h.get("x-ratelimit-reset-requests"))
        reset_tok = _parse_duration(h.get("x-ratelimit-reset-tokens"))

        return ParsedRateLimit(
            provider="openai",
            remaining_requests=rem_req,
            limit_requests=limit_req,
            remaining_tokens=rem_tok,
            limit_tokens=limit_tok,
            reset_requests_seconds=reset_req,
            reset_tokens_seconds=reset_tok,
            retry_after_seconds=retry_after,
        )

    @classmethod
    def _extract_anthropic(cls, h: Dict[str, str], retry_after: Optional[float]) -> ParsedRateLimit:
        rem_req = _safe_int(h.get("anthropic-ratelimit-requests-remaining"))
        limit_req = _safe_int(h.get("anthropic-ratelimit-requests-limit"))
        rem_tok = _safe_int(h.get("anthropic-ratelimit-tokens-remaining"))
        limit_tok = _safe_int(h.get("anthropic-ratelimit-tokens-limit"))
        reset_req = _parse_duration(h.get("anthropic-ratelimit-requests-reset"))
        reset_tok = _parse_duration(h.get("anthropic-ratelimit-tokens-reset"))

        return ParsedRateLimit(
            provider="anthropic",
            remaining_requests=rem_req,
            limit_requests=limit_req,
            remaining_tokens=rem_tok,
            limit_tokens=limit_tok,
            reset_requests_seconds=reset_req,
            reset_tokens_seconds=reset_tok,
            retry_after_seconds=retry_after,
        )

    @classmethod
    def _extract_gemini(
        cls,
        h: Dict[str, str],
        retry_after: Optional[float],
        status_code: int,
        response_body: Optional[Any],
    ) -> ParsedRateLimit:
        # Check if Retry-After is specified in error body (Google API format)
        extracted_retry = retry_after
        if extracted_retry is None and isinstance(response_body, dict):
            err = response_body.get("error", {})
            msg = str(err.get("message", ""))
            # e.g. "Quota exceeded for quota metric... please retry after 15s"
            m = re.search(r"retry(?:\s+after)?\s+(\d+(?:\.\d+)?)\s*s", msg, re.IGNORECASE)
            if m:
                extracted_retry = float(m.group(1))

        # Check standard x-goog headers
        rem_req = _safe_int(h.get("x-ratelimit-remaining"))
        return ParsedRateLimit(
            provider="gemini",
            remaining_requests=rem_req,
            retry_after_seconds=extracted_retry,
        )

    @classmethod
    def _extract_openrouter(cls, h: Dict[str, str], retry_after: Optional[float]) -> ParsedRateLimit:
        rem_req = _safe_int(h.get("x-ratelimit-remaining"))
        limit_req = _safe_int(h.get("x-ratelimit-limit"))
        reset_sec = _parse_duration(h.get("x-ratelimit-reset"))

        return ParsedRateLimit(
            provider="openrouter",
            remaining_requests=rem_req,
            limit_requests=limit_req,
            reset_requests_seconds=reset_sec,
            retry_after_seconds=retry_after,
        )

    @classmethod
    def _extract_generic(cls, provider: str, h: Dict[str, str], retry_after: Optional[float]) -> ParsedRateLimit:
        rem_req = _safe_int(h.get("x-ratelimit-remaining") or h.get("ratelimit-remaining"))
        limit_req = _safe_int(h.get("x-ratelimit-limit") or h.get("ratelimit-limit"))
        reset_sec = _parse_duration(h.get("x-ratelimit-reset") or h.get("ratelimit-reset"))

        return ParsedRateLimit(
            provider=provider,
            remaining_requests=rem_req,
            limit_requests=limit_req,
            reset_requests_seconds=reset_sec,
            retry_after_seconds=retry_after,
        )
