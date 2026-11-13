"""
Normalisation utilities: phone, date, decimal, header, PII hashing.
All functions are pure (no DB calls) and heavily tested.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from solution.config import PII_SALT

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Header normalisation ────────────────────────────────────────────────────

def norm_header(h: str) -> str:
    """Lowercase, strip, replace runs of underscores/whitespace with one space."""
    h = h.lower().strip()
    h = re.sub(r"[_\s]+", " ", h)
    return h


# ─── Phone normalisation ─────────────────────────────────────────────────────

def normalize_phone(raw: str) -> Optional[str]:
    """
    Normalise an Indian mobile number to '+91XXXXXXXXXX'.
    Returns None if the number is invalid.

    Rules (spec):
    - Strip every non-digit character.
    - If 12 digits starting with 91 → drop the 91.
    - If 11 digits starting with 0 → drop the 0.
    - Valid iff exactly 10 digits and first digit ∈ {6,7,8,9}.
    """
    if not raw or not raw.strip():
        return None
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def hash_phone(canonical: str) -> str:
    """SHA-256(salt + canonical_phone). Used as the join key stored in DB."""
    return hashlib.sha256(f"{PII_SALT}{canonical}".encode()).hexdigest()


def mask_phone(canonical: str) -> str:
    """'+91******' + last 4 digits."""
    return f"+91******{canonical[-4:]}"


# ─── Date parsing ────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(raw: str) -> Optional[str]:
    """
    Parse a date string into 'YYYY-MM-DD'. Returns None if invalid.

    Accepted formats (spec):
    - YYYY-MM-DD
    - DD/MM/YYYY
    - DD-MM-YYYY
    - DD-Mon-YYYY  (e.g. 01-Dec-2026, case-insensitive)
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()

    # Try YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _validate_date(y, mo, d)

    # Try DD/MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _validate_date(y, mo, d)

    # Try DD-MM-YYYY
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})-(\d{4})", raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _validate_date(y, mo, d)

    # Try DD-Mon-YYYY (e.g. 01-Dec-2026)
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", raw)
    if m:
        d, mon_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _MONTH_MAP.get(mon_str)
        if mo is None:
            return None
        return _validate_date(y, mo, d)

    return None


def _validate_date(y: int, mo: int, d: int) -> Optional[str]:
    """Return 'YYYY-MM-DD' if valid calendar date, else None."""
    try:
        datetime(y, mo, d)
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


# ─── Decimal cleaning ────────────────────────────────────────────────────────

_CURRENCY_PREFIX = re.compile(
    r"^(₹|Rs\.?|INR)\s*", re.IGNORECASE
)


def parse_decimal(raw: str) -> Optional[str]:
    """
    Parse a currency/decimal string to 2-decimal-place string.
    Strips ₹, Rs, Rs., INR prefix and comma thousands separators.
    Returns None if invalid or negative.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    raw = _CURRENCY_PREFIX.sub("", raw)   # strip currency prefix
    raw = raw.replace(",", "")            # remove thousands separators
    raw = raw.strip()
    try:
        val = Decimal(raw)
        if val < 0:
            return None
        return str(val.quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


# ─── Enum normalisation ──────────────────────────────────────────────────────

def parse_enum(raw: str, values: list[str]) -> Optional[str]:
    """Case-insensitive match; stored lower-case. Returns None if no match."""
    if not raw or not raw.strip():
        return None
    low = raw.strip().lower()
    if low in values:
        return low
    return None


# ─── Timestamp parsing ───────────────────────────────────────────────────────

def parse_timestamp(raw: str) -> Optional[datetime]:
    """
    Parse event timestamps in three formats:
    - UTC:    '2026-10-05T13:20:21Z'
    - Offset: '2026-10-05T13:20:21+05:30'
    - Naive:  '2026-10-03 10:01:00'  → treated as IST per spec
    Returns aware datetime (UTC-normalised).
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.endswith("Z"):
        try:
            dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if "T" in raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    # Naive datetime → IST
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=IST)
    except ValueError:
        return None


def to_utc_iso(dt: datetime) -> str:
    """Convert aware datetime to UTC ISO-8601 string."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ist_iso(dt: datetime) -> str:
    """Convert aware datetime to IST ISO-8601 with offset."""
    ist = dt.astimezone(IST)
    return ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def to_ist_date(dt: datetime) -> str:
    """Return IST date string 'YYYY-MM-DD' from an aware datetime."""
    return dt.astimezone(IST).strftime("%Y-%m-%d")


# ─── PII encryption (optional; falls back to hash-only if key absent) ────────

def encrypt_name(name: str) -> str:
    """
    Minimal PII protection for customer_name.
    Uses AES-256-GCM if pycryptodome is available;
    falls back to a salted SHA-256 hex (non-reversible but deterministic).
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        import base64
        key = hashlib.sha256(PII_SALT.encode()).digest()   # 32 bytes
        cipher = AES.new(key, AES.MODE_GCM)
        ct, tag = cipher.encrypt_and_digest(name.encode("utf-8"))
        blob = cipher.nonce + tag + ct
        return base64.b64encode(blob).decode()
    except ImportError:
        # Fallback: salted hash (irreversible) – name can't be recovered, but
        # that's acceptable since name is not needed in any report output.
        return hashlib.sha256(f"{PII_SALT}name:{name}".encode()).hexdigest()

