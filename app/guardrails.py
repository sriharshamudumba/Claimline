"""Deterministic guardrails around the voice agent.

Two directions, both independent of the model's good behavior:

  Input  (caller -> claim file): every string the agent sends to a tool is scanned before the
         engine stores it. Card numbers, SSNs, bank account details, passwords and one-time codes
         are removed. Text that tries to instruct the system ("ignore your instructions",
         "mark it approved") is kept as what the caller said but flagged, and never acted on.

  Output (agent -> caller): every agent turn is checked after it is spoken. Promises about
         coverage, payment or fault, quoted limits, ransom advice, mentions of internal fraud
         checks, and claims to be human are caught, and the client asks the agent to correct
         itself in its next breath.

The prompt asks the model to behave; these rules hold even when it doesn't.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- input: sensitive data

_DIGIT_RUN = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}\b")
_SSN_CONTEXT = re.compile(r"\b(social security|ssn|social)\b[^0-9]{0,30}(\d[\d -]{7,10}\d)", re.I)
_ACCOUNT = re.compile(
    r"\b(account|acct|routing|aba|iban|swift)\b(?:\s*(?:number|no\.?|#))?\s*(?:is|:)?\s*([A-Z0-9][A-Z0-9 -]{4,30}\d)", re.I
)
_SECRET = re.compile(
    r"\b(password|passcode|pin|one[- ]time code|verification code|security code|cvv|cvc)\b\s*(?:is|was|:)?\s*(\S+)", re.I
)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, kinds_removed). Conservative: phone, policy, claim, DOT and dollar
    figures are left alone; only things that look like payment or identity secrets are cut."""
    found: list[str] = []

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card_number")
            return "[card number removed]"
        return m.group(0)

    text = _DIGIT_RUN.sub(card, text)

    def ssn_ctx(m: re.Match) -> str:
        found.append("ssn")
        return m.group(0).replace(m.group(2), "[SSN removed]")

    text = _SSN_CONTEXT.sub(ssn_ctx, text)

    def ssn(m: re.Match) -> str:
        found.append("ssn")
        return "[SSN removed]"

    text = _SSN.sub(ssn, text)

    def account(m: re.Match) -> str:
        if sum(c.isdigit() for c in m.group(2)) < 5:
            return m.group(0)
        found.append("bank_account")
        return m.group(0).replace(m.group(2), "[account number removed]")

    text = _ACCOUNT.sub(account, text)

    def secret(m: re.Match) -> str:
        found.append("secret")
        return m.group(0).replace(m.group(2), "[removed]")

    text = _SECRET.sub(secret, text)
    return text, sorted(set(found))


# --------------------------------------------------------------------------- input: instructions

_INJECTION = re.compile(
    r"ignore (?:all |any |your |my |the |previous |prior )*(?:instructions|rules|guidelines|prompt)"
    r"|disregard (?:all |your |the |previous )*(?:instructions|rules)"
    r"|(?:system|developer) (?:prompt|message|mode)"
    r"|you are now\b|act as (?:an? )?(?:adjuster|underwriter|admin)"
    r"|(?:mark|set|flag) (?:it|this|the claim|my claim) (?:as )?(?:approved|paid|covered|closed)"
    r"|approve (?:it|this|the claim|my claim)"
    r"|(?:delete|remove|clear) (?:the |all |any )?(?:flags?|notes?|record)",
    re.I,
)


def find_injection(text: str) -> str | None:
    m = _INJECTION.search(text or "")
    return m.group(0) if m else None


def scan_args(value: Any) -> tuple[Any, list[str], list[str]]:
    """Walk tool arguments. Returns (cleaned_value, sensitive_kinds, injection_phrases)."""
    kinds: list[str] = []
    phrases: list[str] = []

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            clean, k = redact(v)
            kinds.extend(k)
            hit = find_injection(clean)
            if hit:
                phrases.append(hit)
            return clean
        if isinstance(v, dict):
            return {key: walk(x) for key, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(value), sorted(set(kinds)), phrases


# --------------------------------------------------------------------------- output: agent turns

AGENT_RULES: list[tuple[str, re.Pattern, str]] = [
    ("coverage_promise",
     re.compile(r"\byou(?:'re| are) (?:fully |definitely |totally )?covered\b"
                r"|(?<!whether )(?<!if )\b(?:this|that|it)(?:'s| is| will be) (?:fully |definitely )?covered\b"
                r"|\b(?:this|that|it|you)(?:'s| is| are|'re)? ?not covered\b|\bwon't be covered\b"
                r"|\b(?:claim|it)(?:'s| is| has been| was) (?:been )?(?:approved|denied)\b", re.I),
     "You stated or implied a coverage decision. Say plainly that coverage is decided by an adjuster, not you."),
    ("payment_promise",
     re.compile(r"\b(?:we|they|the carrier|your policy)(?:'ll| will| are going to) (?:pay|cover|reimburse|refund)\b"
                r"|\byou(?:'ll| will) (?:get|be) (?:paid|reimbursed|your money back)\b", re.I),
     "You promised a payment. Correct it: an adjuster decides what is paid, and you can't promise an amount or outcome."),
    ("fault_opinion",
     re.compile(r"\b(?:it|this)(?:'s| is| was)(?: not)? (?:your|their|his|her) fault\b"
                r"|\b(?:it|this) (?:was|wasn't|isn't|is not) (?:your|their|his|her) fault\b"
                r"|\byou(?:'re| are| were)(?: not)? (?:at fault|liable)\b"
                r"|\b(?:the other (?:driver|party)|they) (?:was|were|is|are) at fault\b", re.I),
     "You gave an opinion on fault. Correct it: fault isn't something you decide; an adjuster reviews everything."),
    ("limit_quote",
     re.compile(r"\byour (?:limit|deductible|sublimit|retention) (?:is|would be|will be)\b"
                r"|\$?\d[\d,.]*\s*(?:k|thousand|million)?\s*(?:dollar )?(?:deductible|limit|sublimit|retention)\b", re.I),
     "You quoted a limit or deductible. Correct it: the adjuster or the policy documents confirm those, not you."),
    ("ransom_advice",
     re.compile(r"\b(?:you should|go ahead and|just|it's best to|better to|I'd) pay (?:the )?(?:ransom|them|the attackers|the bitcoin)\b", re.I),
     "You suggested paying. Correct it: don't pay or contact the attackers; the breach coach and the carrier decide that together."),
    ("internal_disclosure",
     re.compile(r"\b(?:fraud (?:check|review|flag|team)|special investigations?|SIU|red flag|flagged (?:you|your claim) for)\b", re.I),
     "You mentioned internal reviews. Don't discuss them. Move on to your next question without referring to it."),
    ("claims_human",
     re.compile(r"\bI(?:'m| am) (?:a )?(?:real |actual )?(?:person|human)\b(?! assistant)"
                r"|\bI'm not (?:an? )?(?:AI|bot|robot|machine)\b", re.I),
     "You claimed to be human. Correct it now: you're an AI assistant for the claims team, and you can connect them to a person."),
]


def check_agent_turn(text: str) -> list[dict]:
    hits = []
    for code, pattern, fix in AGENT_RULES:
        m = pattern.search(text or "")
        if m:
            hits.append({"code": code, "matched": m.group(0), "correction": fix})
    return hits


SENSITIVE_LABEL = {
    "card_number": "a card number",
    "ssn": "a Social Security number",
    "bank_account": "a bank account number",
    "secret": "a password or code",
}

AGENT_LABEL = {
    "coverage_promise": "Agent hinted at a coverage decision",
    "payment_promise": "Agent promised a payment",
    "fault_opinion": "Agent gave an opinion on fault",
    "limit_quote": "Agent quoted a limit or deductible",
    "ransom_advice": "Agent suggested paying a ransom",
    "internal_disclosure": "Agent mentioned an internal review",
    "claims_human": "Agent claimed to be human",
}
