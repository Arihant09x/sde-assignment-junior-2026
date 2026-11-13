"""
Tests for normalise.py: phone, date, decimal, header, timestamp utilities.
"""
import pytest
from solution.normalise import (
    norm_header,
    normalize_phone,
    hash_phone,
    mask_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
    to_ist_date,
    to_ist_iso,
)
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


# ─── Header normalisation ────────────────────────────────────────────────────

class TestNormHeader:
    def test_lowercase(self):
        # CamelCase is lowercased but not split (spec only replaces _ and whitespace)
        assert norm_header("CustomerName") == "customername"

    def test_lowercase_with_space(self):
        assert norm_header("Customer Name") == "customer name"

    def test_trim(self):
        assert norm_header("  phone  ") == "phone"

    def test_underscore_to_space(self):
        assert norm_header("policy_number") == "policy number"

    def test_multiple_underscores(self):
        assert norm_header("mobile__no") == "mobile no"

    def test_mixed_whitespace_underscore(self):
        assert norm_header("mobile _no") == "mobile no"

    def test_alias_insured_name(self):
        assert norm_header("Insured Name") == "insured name"

    def test_alias_mobile_no(self):
        assert norm_header("Mobile No") == "mobile no"


# ─── Phone normalisation ────────────────────────────────────────────────────

class TestNormalizePhone:
    # Valid formats
    def test_bare_10_digit_6(self):
        assert normalize_phone("6789012345") == "+916789012345"

    def test_bare_10_digit_7(self):
        assert normalize_phone("7001234567") == "+917001234567"

    def test_bare_10_digit_8(self):
        assert normalize_phone("8001234567") == "+918001234567"

    def test_bare_10_digit_9(self):
        assert normalize_phone("9001234567") == "+919001234567"

    def test_plus_91_prefix(self):
        assert normalize_phone("+916463745499") == "+916463745499"

    def test_plus_91_space_format(self):
        assert normalize_phone("+91 64637 45499") == "+916463745499"

    def test_zero_prefix_11_digits(self):
        assert normalize_phone("09001234567") == "+919001234567"

    def test_91_dash_format(self):
        assert normalize_phone("91-77187-50036") == "+917718750036"

    def test_plus91_no_space(self):
        assert normalize_phone("+919001234567") == "+919001234567"

    def test_parenthetical_format(self):
        assert normalize_phone("(+91) 9001234567") == "+919001234567"

    def test_12_digits_91_prefix(self):
        # 91 + 10 valid digits
        assert normalize_phone("919001234567") == "+919001234567"

    # Invalid formats
    def test_9_digits(self):
        assert normalize_phone("900123456") is None  # only 9 digits

    def test_bad_prefix_5(self):
        assert normalize_phone("5001234567") is None  # starts with 5

    def test_landline(self):
        assert normalize_phone("022-2345-6789") is None

    def test_na(self):
        assert normalize_phone("N/A") is None

    def test_us_number(self):
        assert normalize_phone("+1 415 555 0134") is None

    def test_empty(self):
        assert normalize_phone("") is None

    def test_none(self):
        assert normalize_phone(None) is None

    def test_whitespace_only(self):
        assert normalize_phone("   ") is None

    def test_12_digits_not_starting_91(self):
        # 12 digits not starting with 91 → invalid
        assert normalize_phone("129001234567") is None


class TestPhoneMasking:
    def test_mask(self):
        assert mask_phone("+919001234567") == "+91******4567"

    def test_mask_different(self):
        assert mask_phone("+916789012345") == "+91******2345"


# ─── Date parsing ────────────────────────────────────────────────────────────

class TestParseDate:
    def test_yyyy_mm_dd(self):
        assert parse_date("2026-11-15") == "2026-11-15"

    def test_dd_slash_mm_yyyy(self):
        assert parse_date("15/11/2026") == "2026-11-15"

    def test_dd_dash_mm_yyyy(self):
        assert parse_date("15-11-2026") == "2026-11-15"

    def test_dd_mon_yyyy_lower(self):
        assert parse_date("15-Nov-2026") == "2026-11-15"

    def test_dd_mon_yyyy_upper(self):
        assert parse_date("01-DEC-2026") == "2026-12-01"

    def test_dd_mon_yyyy_mixed(self):
        assert parse_date("01-Dec-2026") == "2026-12-01"

    def test_invalid_day(self):
        assert parse_date("31/02/2026") is None  # Feb 31 doesn't exist

    def test_invalid_month(self):
        assert parse_date("2026-13-01") is None

    def test_text_value(self):
        assert parse_date("soon") is None

    def test_tbd(self):
        assert parse_date("TBD") is None

    def test_empty(self):
        assert parse_date("") is None

    def test_none(self):
        assert parse_date(None) is None


# ─── Decimal parsing ─────────────────────────────────────────────────────────

class TestParseDecimal:
    def test_plain_float(self):
        assert parse_decimal("12345.67") == "12345.67"

    def test_plain_int(self):
        assert parse_decimal("12345") == "12345.00"

    def test_rupee_symbol(self):
        assert parse_decimal("₹27,554") == "27554.00"

    def test_rs_prefix(self):
        assert parse_decimal("Rs. 18000") == "18000.00"

    def test_rs_no_space(self):
        assert parse_decimal("Rs.18000") == "18000.00"

    def test_inr_prefix(self):
        assert parse_decimal("INR 18678.3") == "18678.30"

    def test_comma_separator(self):
        assert parse_decimal("1,23,456.78") == "123456.78"

    def test_zero(self):
        assert parse_decimal("0") == "0.00"

    def test_negative(self):
        assert parse_decimal("-100") is None

    def test_text(self):
        assert parse_decimal("N/A") is None

    def test_dash(self):
        assert parse_decimal("-") is None

    def test_words(self):
        assert parse_decimal("twelve thousand") is None

    def test_empty(self):
        assert parse_decimal("") is None


# ─── Enum parsing ────────────────────────────────────────────────────────────

class TestParseEnum:
    VALUES = ["en", "hi", "ta", "kn", "te", "mr"]

    def test_exact(self):
        assert parse_enum("en", self.VALUES) == "en"

    def test_uppercase(self):
        assert parse_enum("EN", self.VALUES) == "en"

    def test_whitespace(self):
        assert parse_enum(" hi ", self.VALUES) == "hi"

    def test_invalid(self):
        assert parse_enum("Hindi", self.VALUES) is None

    def test_english_full(self):
        assert parse_enum("english", self.VALUES) is None

    def test_empty(self):
        assert parse_enum("", self.VALUES) is None


# ─── Timestamp parsing ───────────────────────────────────────────────────────

class TestParseTimestamp:
    def test_utc_z(self):
        dt = parse_timestamp("2026-10-05T13:20:21Z")
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2026
        assert dt.hour == 13

    def test_offset_ist(self):
        dt = parse_timestamp("2026-10-05T13:20:21+05:30")
        assert dt is not None
        # Convert to UTC: 13:20:21 IST = 07:50:21 UTC
        utc = dt.astimezone(timezone.utc)
        assert utc.hour == 7
        assert utc.minute == 50

    def test_naive_treated_as_ist(self):
        dt = parse_timestamp("2026-10-03 10:01:00")
        assert dt is not None
        # IST naive → UTC = 10:01 IST = 04:31 UTC
        utc = dt.astimezone(timezone.utc)
        assert utc.hour == 4
        assert utc.minute == 31

    def test_invalid(self):
        assert parse_timestamp("not-a-date") is None

    def test_empty(self):
        assert parse_timestamp("") is None


class TestToIstDate:
    def test_utc_converts(self):
        dt = datetime(2026, 10, 5, 20, 0, 0, tzinfo=timezone.utc)
        # 20:00 UTC = 01:30 IST next day (Oct 6)
        assert to_ist_date(dt) == "2026-10-06"

    def test_mid_day_same(self):
        dt = datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc)
        # 10:00 UTC = 15:30 IST same day
        assert to_ist_date(dt) == "2026-10-05"
