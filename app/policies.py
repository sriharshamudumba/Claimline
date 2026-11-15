"""Mock policy admin system. All insureds, people and vendors are fictional."""

from __future__ import annotations

import re
from datetime import date

POLICIES: dict[str, dict] = {
    # Small trucking fleet: auto liability, physical damage, cargo (occurrence forms)
    "CT20417": {
        "policy_number": "CT-20417",
        "insured": "XYZ Freight LLC",
        "program": "trucking",
        "status": "active",
        "usdot": "9900417",
        "term_start": date(2026, 7, 15),
        "term_end": date(2027, 7, 15),
        "coverages": {
            "auto_liability": {"limit": 1_000_000, "form": "occurrence"},
            "physical_damage": {"deductible": 2_500, "form": "occurrence"},
            "cargo": {"limit": 100_000, "deductible": 1_000, "form": "occurrence"},
        },
        "units": [
            {"id": "T1", "description": "2022 Freightliner Cascadia", "vin_last4": "3391"},
            {"id": "T2", "description": "2020 Peterbilt 579", "vin_last4": "7712"},
        ],
        "drivers": ["Marcus Hill", "Ana Torres"],
        "authorized_contacts": ["Marcus Hill", "Ana Torres", "Jen Okafor"],
        "prior_claims_12mo": 1,
    },
    # Cancelled trucking policy, for the "loss after cancellation" case
    "CT17003": {
        "policy_number": "CT-17003",
        "insured": "DEF Haul Co",
        "program": "trucking",
        "status": "cancelled",
        "cancel_date": date(2026, 10, 1),
        "usdot": "9900522",
        "term_start": date(2026, 1, 10),
        "term_end": date(2027, 1, 10),
        "coverages": {
            "auto_liability": {"limit": 750_000, "form": "occurrence"},
            "physical_damage": {"deductible": 5_000, "form": "occurrence"},
        },
        "units": [{"id": "T1", "description": "2018 Volvo VNL", "vin_last4": "0458"}],
        "drivers": ["Sam Reyes"],
        "authorized_contacts": ["Sam Reyes"],
        "prior_claims_12mo": 0,
    },
    # Series A startup with the full stack (claims-made liability lines)
    "TS88120": {
        "policy_number": "TS-88120",
        "insured": "ABC Labs Inc",
        "program": "startup",
        "status": "active",
        "term_start": date(2026, 4, 1),
        "term_end": date(2027, 4, 1),
        "coverages": {
            "cyber": {
                "limit": 1_000_000,
                "retention": 10_000,
                "form": "claims_made",
                "sublimits": {"social_engineering": 100_000, "extortion": 1_000_000},
            },
            "tech_eo": {
                "limit": 1_000_000,
                "aggregate": 2_000_000,
                "retention": 10_000,
                "form": "claims_made",
                "retro_date": date(2025, 2, 1),
            },
            "dno": {"limit": 2_000_000, "retention": 25_000, "form": "claims_made"},
            "cgl": {"limit": 1_000_000, "form": "occurrence"},
        },
        "authorized_contacts": ["Priya Shah", "Dan Wu"],
        "prior_claims_12mo": 0,
    },
    # Brand-new seed policy, D&O and CGL only (no cyber)
    "TS91002": {
        "policy_number": "TS-91002",
        "insured": "PQR AI Inc",
        "program": "startup",
        "status": "active",
        "term_start": date(2026, 11, 1),
        "term_end": date(2027, 11, 1),
        "coverages": {
            "dno": {"limit": 1_000_000, "retention": 10_000, "form": "claims_made"},
            "cgl": {"limit": 1_000_000, "form": "occurrence"},
        },
        "authorized_contacts": ["Leo Park"],
        "prior_claims_12mo": 0,
    },
}

EXISTING_CLAIMS = [
    {
        "claim_number": "CLM-260902-4417",
        "policy_key": "CT20417",
        "line": "trucking",
        "loss_date": "2026-09-02",
        "summary": "Cargo shortage at delivery, Sacramento",
        "filed": "2026-09-03",
        "status": "open",
        "team": "Cargo claims, adjuster Dana Ortiz",
        "next_step": "Waiting on the signed proof of loss from the insured before the claim can be valued.",
        "outstanding": ["Signed proof of loss", "Delivery receipt with the shortage noted"],
    }
]

# Approved incident-response vendors (fictional)
PANEL_VENDORS = ["RST IR", "UVW Forensics", "OPQ Breach Counsel"]

_SUFFIXES = re.compile(r"\b(llc|inc|incorporated|corp|corporation|co|company|ltd)\b")


def normalize_policy_number(raw: str | None) -> str:
    """Speech-to-text gives 'c t 2 0 4 1 7' or 'CT-20417'. Strip to alphanumerics."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def normalize_name(raw: str | None) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (raw or "").lower())
    s = _SUFFIXES.sub(" ", s)
    # Speech-to-text spells initials out ("x y z freight"); join them back ("xyz freight").
    s = re.sub(r"\b(?:[a-z] )+[a-z]\b", lambda m: m.group(0).replace(" ", ""), " ".join(s.split()))
    return s


def name_matches(spoken: str | None, insured: str) -> bool:
    a, b = normalize_name(spoken), normalize_name(insured)
    return bool(a) and (a == b or a in b or b in a)


def find_policy(policy_number: str | None = None, usdot: str | None = None, business_name: str | None = None):
    """Return (key, policy) or (None, None). A name-only match must be unique."""
    if policy_number:
        key = normalize_policy_number(policy_number)
        return (key, POLICIES[key]) if key in POLICIES else (None, None)
    if usdot:
        digits = re.sub(r"\D", "", usdot)
        for key, p in POLICIES.items():
            if p.get("usdot") == digits:
                return key, p
        return None, None
    if business_name:
        hits = [(k, p) for k, p in POLICIES.items() if name_matches(business_name, p["insured"])]
        if len(hits) == 1:
            return hits[0]
    return None, None
