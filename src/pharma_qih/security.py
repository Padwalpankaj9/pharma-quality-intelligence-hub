"""AI security guardrails: Unicode normalization, injection blocking, output sanitization, RBAC."""

from __future__ import annotations

import hmac
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from pharma_qih.config import DEFAULT_API_KEYS, METADATA_DIR

# Patterns that signal an attempt to hijack the agent or steal hidden instructions
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"(reveal|show|dump|print)\s+(your\s+)?(system\s+prompt|secrets|api\s*key)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(developer|admin|jailbreak)\s+mode", re.IGNORECASE),
    re.compile(r"bypass\s+(security|guardrails|access\s+control|rbac)", re.IGNORECASE),
    re.compile(r"exfiltrate|send\s+data\s+to\s+http", re.IGNORECASE),
]

# Patterns that signal a destructive or unauthorized SQL payload
SQL_INJECTION_PATTERNS = [
    re.compile(r";\s*(drop|delete|update|insert|alter|truncate)\s+", re.IGNORECASE),
    re.compile(r"\bunion\s+(all\s+)?select\b", re.IGNORECASE),
    re.compile(r"\bor\s+1\s*=\s*1\b", re.IGNORECASE),
    re.compile(r"--\s*$"),
]

# Patterns for sensitive values that must never appear in outputs
PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED-EMAIL]"),
    (re.compile(r"\b(sk-|gho_|ghp_|dapi)[A-Za-z0-9_\-]{12,}\b"), "[REDACTED-TOKEN]"),
]

# Patterns for unsafe HTML/script tags and markdown image exfiltration in retrieved text
HTML_TAG_PATTERN = re.compile(r"<\s*/?\s*(script|iframe|img|svg|object|embed)[^>]*>", re.IGNORECASE)
MD_IMAGE_EXFIL_PATTERN = re.compile(r"!\[[^\]]*\]\(https?://[^)]+\)", re.IGNORECASE)
INVISIBLE_CHARS_PATTERN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


@dataclass(frozen=True)
class SecurityCheckResult:
    """Outcome of running input guardrails on a user query."""

    allowed: bool
    reason: str | None = None


def normalize_for_security(text: str) -> str:
    """Normalize Unicode homoglyphs and strip invisible characters before matching."""
    normalized = unicodedata.normalize("NFKC", text)
    return INVISIBLE_CHARS_PATTERN.sub("", normalized).strip()


def check_input_safety(text: str) -> SecurityCheckResult:
    """Block direct prompt-injection and SQL-injection payloads before they hit tools."""
    cleaned = normalize_for_security(text)
    if not cleaned:
        return SecurityCheckResult(allowed=False, reason="empty_input")
    if len(cleaned) > 1500:
        return SecurityCheckResult(allowed=False, reason="input_too_long")

    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(cleaned):
            return SecurityCheckResult(allowed=False, reason="prompt_injection_detected")

    for pattern in SQL_INJECTION_PATTERNS:
        if pattern.search(cleaned):
            return SecurityCheckResult(allowed=False, reason="sql_injection_detected")

    return SecurityCheckResult(allowed=True, reason=None)


def redact_sensitive_text(text: str) -> str:
    """Scrub emails, SSNs, and API tokens from text before returning it."""
    redacted = text
    for pattern, replacement in PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def sanitize_retrieved_text(text: str) -> str:
    """Neutralize indirect prompt injection, HTML tags, and markdown image exfiltration."""
    cleaned = redact_sensitive_text(normalize_for_security(text))
    cleaned = HTML_TAG_PATTERN.sub("[SANITIZED-TAG]", cleaned)
    cleaned = MD_IMAGE_EXFIL_PATTERN.sub("[SANITIZED-IMAGE]", cleaned)
    for pattern in PROMPT_INJECTION_PATTERNS:
        cleaned = pattern.sub("[SANITIZED-INSTRUCTION]", cleaned)
    return cleaned


@lru_cache(maxsize=1)
def load_access_policy() -> dict[str, Any]:
    """Read the FAIR RBAC access policy once and cache it."""
    policy_path = METADATA_DIR / "access_policy.yaml"
    with policy_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve_role(api_key: str | None) -> str | None:
    """Map an incoming API key to its RBAC role using constant-time comparison."""
    if not api_key:
        return None
    matched_role: str | None = None
    for candidate_key, role in DEFAULT_API_KEYS.items():
        if hmac.compare_digest(api_key, candidate_key):
            matched_role = role
    return matched_role


def is_endpoint_allowed(role: str, path: str) -> bool:
    """Check whether a role is allowed to call a given REST endpoint."""
    policy = load_access_policy()
    role_cfg = policy.get("roles", {}).get(role)
    if not role_cfg:
        return False
    allowed = role_cfg.get("allowed_endpoints", [])
    return "*" in allowed or path in allowed


def is_mcp_tool_allowed(role: str, tool_name: str) -> bool:
    """Check whether a role is allowed to call a given MCP or agent tool."""
    policy = load_access_policy()
    role_cfg = policy.get("roles", {}).get(role)
    if not role_cfg:
        return False
    allowed = role_cfg.get("allowed_mcp_tools", [])
    return "*" in allowed or tool_name in allowed
