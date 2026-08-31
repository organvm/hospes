"""Shared admission checks for text that HOSPES is allowed to persist.

HOSPES keeps operational summaries and opaque owner references.  Contact
values, copied correspondence, signatures, and private agreement material stay
in their external owners.  These helpers deliberately return only a category;
callers must never echo the rejected value into logs or API errors.
"""

from __future__ import annotations

import re


MAX_INSPECTION_LENGTH = 1000
EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@"
    r"(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}\b"
)
PHONE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?\d{1,3}[ .-])?"
    r"(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{4}"
    r"(?![A-Za-z0-9])"
)
COMPACT_PHONE = re.compile(r"(?<![A-Za-z0-9])\+?\d{10,15}(?![A-Za-z0-9])")
LOCAL_PHONE = re.compile(r"(?<![A-Za-z0-9])\d{3}[ .-]\d{4}(?![A-Za-z0-9])")

PRIVATE_TEXT = re.compile(
    r"(?:"
    r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|SIGNED)|"
    r"(?im:^\s*(?:from|to|cc|bcc|subject):\s+\S)|"
    r"(?im:^\s*(?:dear|hello|hi)\s+[^\n,]{1,80},\s*$)|"
    r"(?i:\b(?:signed by|signature|routing number|account number|tax id)\s*:)|"
    r"(?i:\b(?:correspondence_body|message_body|signed_document|signed_release|"
    r"private_notes|bank_details)\b)|"
    r"(?i:\b(?:this agreement is (?:made|entered)|agreement between the parties|"
    r"between the parties hereto|in witness whereof)\b)|"
    r"(?i:\b(?:hereinafter|party of the first part)\b)"
    r")"
)
_AGREEMENT_DATE = (
    r"(?:\d{4}-\d{2}-\d{2}|(?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\s+\d{1,2},?\s+\d{4})"
)
AGREEMENT_HEADING = re.compile(
    r"^\s*(?:(?:mutual|guest|talent|artist|speaker|host|podcast|media|recording)\s+)*"
    r"(?:confidentiality|non[- ]?disclosure|services?|appearance|release)\s+"
    r"agreement(?P<suffix>[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_AGREEMENT_VERSION = r"version\s+(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9._-]+"
_AGREEMENT_METADATA_CORE = (
    rf"(?:{_AGREEMENT_DATE}|effective\s+immediately|"
    rf"(?:effective(?:\s+as\s+of)?|dated)\s+{_AGREEMENT_DATE}|"
    rf"confidential(?:\s+draft)?|draft|final|executed(?:\s+on\s+"
    rf"{_AGREEMENT_DATE})?|{_AGREEMENT_VERSION})"
)
_AGREEMENT_PARENTHETICAL = (
    rf"\((?:(?:(?:the|this)\s+)?[\"“]?agreement[\"”]?|"
    rf"{_AGREEMENT_METADATA_CORE})\)"
)
AGREEMENT_METADATA = re.compile(
    rf"(?:{_AGREEMENT_METADATA_CORE}|{_AGREEMENT_PARENTHETICAL})[.!]?",
    re.IGNORECASE,
)


def _has_agreement_heading(value: str) -> bool:
    for match in AGREEMENT_HEADING.finditer(value):
        suffix = match.group("suffix").strip()
        if not suffix:
            return True
        metadata = suffix[1:].strip() if suffix[0] in "—–:-" else suffix
        if AGREEMENT_METADATA.fullmatch(metadata):
            return True
    return False


def contact_kind(value: str) -> str | None:
    """Return a generic contact category without exposing the matched value."""
    if len(value) > MAX_INSPECTION_LENGTH:
        return "oversized content"
    if EMAIL.search(value):
        return "email-like content"
    if PHONE.search(value) or COMPACT_PHONE.search(value) or LOCAL_PHONE.search(value):
        return "phone-like content"
    return None


def private_text_kind(value: str) -> str | None:
    """Return the rejected private-content category, if any."""
    contact = contact_kind(value)
    if contact:
        return contact
    if PRIVATE_TEXT.search(value) or _has_agreement_heading(value):
        return "correspondence-, signature-, or agreement-like content"
    return None
