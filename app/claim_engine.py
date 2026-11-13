"""Claim intake and triage engine for a commercial carrier.

Design rule: the LLM runs the conversation, this engine owns the claim.
Every fact goes through a tool call, gets validated here, and every
coverage-relevant judgment (flags, severity, routing, deadlines) is
computed deterministically. The engine triages; adjusters decide.
"""

from __future__ import annotations

import random
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from .policies import EXISTING_CLAIMS, PANEL_VENDORS, POLICIES, find_policy, name_matches

LINES = ("trucking", "cyber", "liability")
CALL_REASONS = ("new_claim", "existing_claim", "policy_question", "certificate_request", "billing", "other")
SERVICE_TOPICS = ("policy_question", "certificate_request", "billing", "other")

REASON_NEXT = {
    "new_claim": "Before anything else, ask if they're okay (and for a crash, whether anyone is hurt). Then their name, "
                 "then identify_caller with caller_ok.",
    "existing_claim": "Get their name (identify_caller), then the claim number and business name for lookup_claim.",
    "policy_question": "Get their name (identify_caller). Don't answer coverage questions yourself; take the details and "
                       "create_service_request so a specialist calls back.",
    "certificate_request": "Get their name (identify_caller), who the certificate is for, any required limits or "
                           "additional insured wording, and the deadline. Then create_service_request.",
    "billing": "Get their name (identify_caller) and what the billing question is, then create_service_request.",
    "other": "Get their name (identify_caller), understand what they need, and create_service_request, or request_human.",
}


def _norm_claim(raw: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


# Claims known to this server: seeded with historical claims, plus anything filed while it runs.
CLAIM_REGISTRY: dict[str, dict] = {}
for _c in EXISTING_CLAIMS:
    CLAIM_REGISTRY[_norm_claim(_c["claim_number"])] = {
        **_c, "insured": POLICIES[_c["policy_key"]]["insured"], "notes": [],
    }
SERVICE_REQUESTS: dict[str, dict] = {}
ROLES = ("policyholder_contact", "driver", "third_party_claimant", "broker")

ENUMS: dict[str, list[str]] = {
    "dispatch_status": ["under_dispatch", "deadhead", "bobtail", "personal_use"],
    "accident_type": [
        "rear_end", "sideswipe", "head_on", "rollover", "jackknife", "backing",
        "fixed_object", "cargo_theft", "cargo_damage", "fire", "other",
    ],
    "incident_type": ["ransomware", "data_breach", "funds_transfer_fraud", "other"],
    "matter_type": ["demand_letter", "lawsuit", "regulatory_inquiry", "potential_claim"],
    "allegation": [
        "product_failure", "ai_output", "data_privacy", "employment",
        "governance", "bodily_injury_property", "other",
    ],
}

# Single source of truth for fields. Tool schemas are generated from this.
FIELD_TYPES: dict[str, str] = {
    # caller wellbeing, asked on every claim before anything else
    "caller_ok": "bool", "wellbeing_note": "text",
    # shared
    "loss_date": "date", "description": "text", "police_report": "bool", "police_report_number": "text",
    # trucking (insured side)
    "loss_time": "time", "location": "text", "state": "text", "accident_type": "enum",
    "unit_id": "unit", "driver_name": "text", "dispatch_status": "enum",
    "injuries": "bool", "injured_count": "int", "injury_treated_away": "bool", "fatality": "bool",
    "third_party_vehicles": "int", "towed": "bool", "tow_yard": "text", "citation_issued": "bool",
    "hazmat": "bool", "cargo_involved": "bool", "cargo_description": "text", "cargo_value": "money",
    "dashcam": "bool",
    # trucking (third-party claimant side)
    "claimant_vehicle": "text", "insured_vehicle_description": "text",
    # cyber
    "incident_type": "enum", "discovered_at": "datetime", "systems_affected": "text",
    "operations_down": "bool", "ransom_demanded": "bool", "ransom_amount": "text", "ransom_paid": "bool",
    "contacted_attacker": "bool", "pii_involved": "bool", "records_estimate": "int",
    "vendor_engaged": "text", "law_enforcement_notified": "bool",
    "wire_amount": "money", "wire_sent_at": "datetime", "wire_international": "bool", "bank_notified": "bool",
    # claims-made liability (Tech E&O, D&O, EPLI)
    "matter_type": "enum", "allegation": "enum", "claimant_name": "text", "claim_received_date": "date",
    "served": "bool", "service_date": "date", "amount_demanded": "money", "alleged_act_date": "date",
    "responded_to_claimant": "bool", "counsel_retained": "bool",
}

FIELD_HELP: dict[str, str] = {
    "loss_time": "24-hour HH:MM",
    "discovered_at": "ISO local datetime YYYY-MM-DDTHH:MM",
    "wire_sent_at": "ISO local datetime YYYY-MM-DDTHH:MM",
    "unit_id": "Unit id from the policy (T1, T2) or UNLISTED",
    "state": "Two-letter US state",
    "vendor_engaged": "Name of any outside IR/forensics/legal firm already engaged, or 'none'",
    "cargo_value": "USD",
    "wire_amount": "USD",
    "amount_demanded": "USD; omit if not stated",
    "ransom_amount": "As stated, e.g. '4 BTC'",
}

# Natural phrasing for the next question, so the agent asks consistently.
ASK: dict[str, str] = {
    "caller_ok": "Before anything else, how are you doing? Are you okay?",
    "loss_date": "What date did this happen?",
    "loss_time": "About what time?",
    "location": "Where did it happen? A highway and mile marker or nearest exit is perfect.",
    "state": "Which state was that in?",
    "injuries": "Was anyone hurt, in any vehicle?",
    "injured_count": "How many people were hurt?",
    "injury_treated_away": "Did anyone get taken from the scene for medical treatment?",
    "fatality": "I have to ask this one: was anyone killed?",
    "description": "In your own words, what happened?",
    "accident_type": "Would you call it a rear-end, sideswipe, or something else?",
    "third_party_vehicles": "How many other vehicles were involved?",
    "unit_id": "Which truck were you in? I see the Cascadia and the Peterbilt on the policy.",
    "driver_name": "Who was driving?",
    "dispatch_status": "Were you under dispatch with a load at the time, or running empty?",
    "towed": "Did any vehicle need to be towed?",
    "tow_yard": "Do you know which tow company or yard has it?",
    "citation_issued": "Did police issue anyone a citation?",
    "hazmat": "Any hazmat on board, or anything leaking?",
    "cargo_involved": "Was the cargo damaged or affected?",
    "cargo_description": "What were you hauling?",
    "cargo_value": "Roughly what is the load worth?",
    "police_report": "Did police come out and take a report?",
    "police_report_number": "Do you have the report number?",
    "dashcam": "Does the truck have a dashcam?",
    "claimant_vehicle": "What were you driving?",
    "insured_vehicle_description": "Can you describe the truck, and any name or number on the door?",
    "incident_type": "What's going on: ransomware, a data breach, or money sent to the wrong place?",
    "discovered_at": "When did you first notice it?",
    "systems_affected": "Which systems are affected?",
    "operations_down": "Is this stopping you from operating right now?",
    "ransom_demanded": "Have the attackers made a demand?",
    "ransom_amount": "How much are they asking for?",
    "ransom_paid": "Has anyone paid anything yet?",
    "contacted_attacker": "Has anyone on your team replied to the attackers?",
    "pii_involved": "Could customer or employee personal data be involved?",
    "records_estimate": "Roughly how many people's records?",
    "vendor_engaged": "Have you brought in any outside security or legal firm yet?",
    "law_enforcement_notified": "Have you reported this to the FBI or police?",
    "wire_amount": "How much was sent?",
    "wire_sent_at": "When did the payment go out?",
    "wire_international": "Was it a domestic or international wire?",
    "bank_notified": "Have you called your bank's fraud line yet?",
    "matter_type": "Is this a demand letter, a lawsuit, a regulator, or something that hasn't turned into a demand yet?",
    "allegation": "What are they saying went wrong?",
    "claimant_name": "Who is making the claim?",
    "claim_received_date": "When did you first receive it?",
    "served": "Have you been formally served with the lawsuit?",
    "service_date": "What date were you served?",
    "alleged_act_date": "When did the issue they're complaining about happen?",
    "responded_to_claimant": "Has anyone on your side responded to them yet?",
    "counsel_retained": "Have you hired a lawyer for this yet?",
}

QUEUE_NEXT_STEP = {
    "rapid_response": "A senior adjuster will call you back shortly to coordinate the scene response.",
    "breach_response": "A breach coach will call you shortly to coordinate the response and approved vendors.",
    "litigation": "A liability adjuster will call you to coordinate defense.",
    "liability_desk": "A liability adjuster will review the matter and call you.",
    "desk_adjuster": "An adjuster will reach out to walk through next steps.",
    "fast_track": "You'll get a confirmation text, and an adjuster will follow up on repairs.",
}

_TRUE = {"true", "yes", "y", "yeah", "yep", "1"}
_FALSE = {"false", "no", "n", "nope", "0", "none"}
SEV = {"low": 0, "medium": 1, "high": 2}
MAX_VERIFY_ATTEMPTS = 3


def _yn(v) -> str:
    return "yes" if v is True else "no" if v is False else "unknown"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p %a %b %-d")


class ClaimSession:
    def __init__(self, session_id: str, now: datetime | Callable[[], datetime] | None = None):
        self.session_id = session_id
        self._now = now
        self.caller: dict | None = None
        self.policy_key: str | None = None
        self.policy: dict | None = None
        self.authorized = False
        self.verify_attempts = 0
        self.line: str | None = None
        self.fields: dict[str, Any] = {}
        self.sticky_flags: list[dict] = []
        self.announced: set[str] = set()
        self.care_given = False
        self.call_reason: str | None = None
        self.call_summary: str | None = None
        self.looked_up_claim: str | None = None
        self.claim_lookup_attempts = 0
        self.service_request: dict | None = None
        self.notes_added: list[dict] = []
        self.status = "intake"
        self.claim_number: str | None = None
        self.events: list[dict] = []

    # ------------------------------------------------------------------ util

    def now(self) -> datetime:
        if self._now is None:
            return datetime.now().replace(second=0, microsecond=0)
        return self._now() if callable(self._now) else self._now

    @property
    def third_party(self) -> bool:
        return bool(self.caller and self.caller["role"] == "third_party_claimant")

    # ----------------------------------------------------------- dispatcher

    def dispatch(self, name: str, args: dict | None) -> dict:
        handlers = {
            "set_call_reason": self.set_call_reason,
            "identify_caller": self.identify_caller,
            "lookup_claim": self.lookup_claim,
            "add_claim_note": self.add_claim_note,
            "create_service_request": self.create_service_request,
            "lookup_policy": self.lookup_policy,
            "open_claim": self.open_claim,
            "update_claim": self.update_claim,
            "flag_for_adjuster": self.flag_for_adjuster,
            "request_human": self.request_human,
            "submit_claim": self.submit_claim,
        }
        handler = handlers.get(name)
        if handler is None:
            result = {"error": f"unknown tool '{name}'"}
        else:
            try:
                result = handler(**(args or {}))
            except TypeError as exc:
                result = {"error": f"bad arguments: {exc}"}
        self.events.append({"at": self.now().isoformat(timespec="minutes"), "tool": name, "args": args, "result": result})
        return result

    # ----------------------------------------------------------------- tools

    def set_call_reason(self, reason: str, summary: str | None = None) -> dict:
        reason = (reason or "").strip().lower()
        if reason not in CALL_REASONS:
            return {"error": "bad_reason", "options": list(CALL_REASONS)}
        self.call_reason = reason
        if summary and summary.strip():
            self.call_summary = summary.strip()[:300]
        return {"ok": True, "next": REASON_NEXT[reason]}

    def lookup_claim(self, claim_number: str, business_name: str) -> dict:
        if not self.caller:
            return {"error": "caller_not_identified", "hint": "Get their name with identify_caller first."}
        if self.claim_lookup_attempts >= MAX_VERIFY_ATTEMPTS:
            return {"found": False, "error": "max_attempts_reached",
                    "hint": "Stop retrying. Offer a callback from the claims team (create_service_request)."}
        self.claim_lookup_attempts += 1
        rec = CLAIM_REGISTRY.get(_norm_claim(claim_number))
        if not rec or not name_matches(business_name, rec["insured"]):
            return {"found": False, "attempts_remaining": MAX_VERIFY_ATTEMPTS - self.claim_lookup_attempts,
                    "hint": "Read the claim number back one character at a time and confirm the business name."}
        self.looked_up_claim = _norm_claim(claim_number)
        return {
            "found": True,
            "claim_number": rec["claim_number"],
            "insured": rec["insured"],
            "line": rec["line"],
            "loss_date": rec["loss_date"],
            "summary": rec["summary"],
            "status": rec["status"],
            "team": rec["team"],
            "next_step": rec["next_step"],
            "outstanding_documents": rec["outstanding"],
            "hint": "Share status, next step and what's outstanding. Don't speculate on payment or coverage. "
                    "Offer to add a note if they have new information.",
        }

    def add_claim_note(self, claim_number: str, note: str) -> dict:
        key = _norm_claim(claim_number)
        if key != self.looked_up_claim:
            return {"error": "claim_not_verified", "hint": "Look up and verify the claim with lookup_claim first."}
        if not (note or "").strip():
            return {"error": "empty_note"}
        entry = {"at": self.now().isoformat(timespec="minutes"), "by": self.caller["name"], "note": note.strip()[:1000]}
        CLAIM_REGISTRY[key]["notes"].append(entry)
        self.notes_added.append(entry)
        return {"ok": True, "say_to_caller": "Confirm the note is on the file and the adjuster will see it."}

    def create_service_request(self, topic: str, details: str, deadline: str | None = None) -> dict:
        if not self.caller:
            return {"error": "caller_not_identified", "hint": "Get their name with identify_caller first."}
        topic = (topic or "").strip().lower()
        if topic not in SERVICE_TOPICS:
            return {"error": "bad_topic", "options": list(SERVICE_TOPICS)}
        if not (details or "").strip():
            return {"error": "missing_details"}
        number = f"SR-{self.now():%y%m%d}-{random.randint(1000, 9999)}"
        self.service_request = {"number": number, "topic": topic, "details": details.strip()[:1000],
                                "deadline": deadline, "caller": self.caller["name"],
                                "callback_number": self.caller.get("callback_number")}
        SERVICE_REQUESTS[number] = self.service_request
        team = {"policy_question": "a policy specialist", "certificate_request": "the certificates team",
                "billing": "billing", "other": "the right team"}[topic]
        return {"created": True, "request_number": number,
                "say_to_caller": f"Give the request number one character at a time and say {team} will follow up"
                                 + (f" before {deadline}." if deadline else ".")}

    def identify_caller(
        self,
        caller_name: str,
        caller_role: str,
        caller_ok: bool | str | None = None,
        wellbeing_note: str | None = None,
        callback_number: str | None = None,
    ) -> dict:
        if not self.call_reason:
            return {"error": "reason_not_set",
                    "hint": "Ask what they're calling about first, then set_call_reason."}
        ok, val = self._validate("caller_ok", caller_ok) if caller_ok is not None else (False, None)
        if self.call_reason == "new_claim" and not ok:
            return {
                "error": "wellbeing_not_checked",
                "hint": "This is a new loss. Ask how the caller is doing first: are they okay, are they hurt? Then pass caller_ok.",
            }
        role = (caller_role or "").strip().lower()
        if role not in ROLES:
            return {"error": "bad_role", "options": list(ROLES)}
        if not (caller_name or "").strip():
            return {"error": "missing_name"}
        self.caller = {"name": caller_name.strip(), "role": role, "callback_number": callback_number}
        if ok:
            self.fields["caller_ok"] = val
        if wellbeing_note and wellbeing_note.strip():
            self.fields["wellbeing_note"] = wellbeing_note.strip()[:500]
        say_now = self._new_say_now(self._evaluate())
        hint = ("Ask them to tell you what happened, in their own words. Get the policy after you've heard the story."
                if self.call_reason == "new_claim" else REASON_NEXT[self.call_reason])
        out = {"ok": True, "say_now": say_now, "next": hint}
        care = self._care_guidance()
        if care:
            out["care_guidance"] = care
        return out

    def lookup_policy(
        self,
        business_name: str,
        policy_number: str | None = None,
        usdot_number: str | None = None,
    ) -> dict:
        if not self.caller:
            return {"error": "caller_not_identified", "hint": "Call identify_caller first."}
        if self.policy:
            return {"matched": True, "already_matched": True, **self._policy_view_for_agent()}
        if self.verify_attempts >= MAX_VERIFY_ATTEMPTS:
            return {"matched": False, "error": "max_attempts_reached",
                    "hint": "Stop retrying. Offer a callback from an adjuster to locate the policy."}

        self.verify_attempts += 1
        key, policy = find_policy(policy_number=policy_number, usdot=usdot_number, business_name=None if (policy_number or usdot_number) else business_name)
        if policy and not name_matches(business_name, policy["insured"]):
            policy = None
        if not policy:
            remaining = MAX_VERIFY_ATTEMPTS - self.verify_attempts
            if remaining == 0:
                self._sticky("policy_not_located", "high", "Could not match caller to a policy after 3 attempts.", "rule")
            return {"matched": False, "attempts_remaining": remaining,
                    "hint": "Read the number back one character at a time and confirm the exact business name."}

        self.policy_key, self.policy = key, policy
        if self.third_party:
            self.authorized = False
        elif self.caller["role"] == "broker":
            self.authorized = True
        else:
            allowed = policy.get("authorized_contacts", []) + policy.get("drivers", [])
            self.authorized = any(name_matches(self.caller["name"], n) for n in allowed)
        if self.line:
            return {"matched": True, **self._policy_view_for_agent(), **self._agent_feedback(self._evaluate())}
        return {"matched": True, **self._policy_view_for_agent(),
                "next": "Ask what happened, then call open_claim with the right line."}

    def open_claim(self, line: str) -> dict:
        if not self.caller:
            return {"error": "caller_not_identified", "hint": "Ask if they're okay and who they are, then identify_caller."}
        if self.status != "intake":
            return {"error": "claim_already_submitted", "claim_number": self.claim_number}
        line = (line or "").strip().lower()
        if line not in LINES:
            return {"error": "bad_line", "options": list(LINES)}
        self.call_reason = "new_claim"
        self.line = line
        ev = self._evaluate()
        return {"opened": True, "line": line, **self._agent_feedback(ev)}

    def update_claim(self, fields: dict) -> dict:
        if not self.line:
            return {"error": "claim_not_open", "hint": "Call open_claim first."}
        if self.status != "intake":
            return {"error": "claim_already_submitted", "claim_number": self.claim_number}
        if not isinstance(fields, dict) or not fields:
            return {"error": "no_fields", "hint": "Put the caller's details inside 'fields'."}

        if fields.get("caller_ok") in (False, "no", "false") and self.fields.get("caller_ok") is not False:
            self.care_given = False
        accepted, rejected = {}, {}
        for key, value in fields.items():
            ok, out = self._validate(key, value)
            if ok:
                self.fields[key] = out
                accepted[key] = out
            else:
                rejected[key] = out

        # loss date + time must not land in the future (catches "10 pm yesterday" mistakes)
        ldt = self._loss_dt()
        if ldt and ldt > self.now() + timedelta(minutes=5):
            self.fields.pop("loss_time", None)
            accepted.pop("loss_time", None)
            rejected["loss_time"] = "Date and time together are in the future. Confirm both with the caller."

        # derived loss dates for non-auto lines
        if self.line == "cyber" and "discovered_at" in self.fields:
            self.fields["loss_date"] = self.fields["discovered_at"][:10]
        if self.line == "liability" and "claim_received_date" in self.fields:
            self.fields["loss_date"] = self.fields["claim_received_date"]

        ev = self._evaluate()
        return {"accepted": accepted, "rejected": rejected, **self._agent_feedback(ev)}

    def flag_for_adjuster(self, reason: str, severity: str = "medium") -> dict:
        severity = severity if severity in SEV else "medium"
        self._sticky("agent_escalation", severity, reason, "agent")
        return {"flagged": True, "hint": "Keep collecting details unless the caller needs to stop."}

    def request_human(self, reason: str) -> dict:
        self._sticky("transfer_requested", "medium", reason, "agent")
        return {
            "ok": True,
            "say_to_caller": "I'll get an adjuster on the line. Let me save what you've told me so you don't have to repeat it.",
            "hint": "If the policy and line are known, call submit_claim with partial true before ending.",
        }

    def submit_claim(self, caller_confirmed: bool = False, partial: bool = False) -> dict:
        if not self.line:
            return {"error": "not_ready", "hint": "Open a claim first."}
        if self.status != "intake":
            return {"error": "claim_already_submitted", "claim_number": self.claim_number}
        missing = self._missing()
        if partial:
            basics = ("caller_ok", "loss_date", "description")
            if any(k not in self.fields for k in basics):
                return {"error": "partial_needs_basics", "missing": [k for k in basics if k not in self.fields]}
        else:
            if missing:
                return {"error": "incomplete_claim", "missing_required": missing, "next_question": self._next_q(missing)}
            if caller_confirmed is not True:
                return {"error": "not_confirmed", "hint": "Read back the key facts and get an explicit yes."}

        if not self.policy:
            self._sticky("policy_not_located", "high", "Submitted before the policy was located. Adjuster to match it.", "rule")
        ev = self._evaluate()
        self.claim_number = f"CLM-{self.now():%y%m%d}-{random.randint(1000, 9999)}"
        self.status = "submitted_incomplete" if partial and missing else "submitted"
        queue = ev["routing"]["queue"]
        CLAIM_REGISTRY[_norm_claim(self.claim_number)] = {
            "claim_number": self.claim_number,
            "policy_key": self.policy_key,
            "insured": (self.policy or {}).get("insured", "unmatched"),
            "line": self.line,
            "loss_date": self.fields.get("loss_date"),
            "summary": self.fields.get("description", ""),
            "filed": self.now().date().isoformat(),
            "status": "open, assigned" if self.status == "submitted" else "open, intake incomplete",
            "team": queue.replace("_", " "),
            "next_step": QUEUE_NEXT_STEP[queue],
            "outstanding": ev["missing_documents"],
            "notes": [],
        }
        return {
            "submitted": True,
            "claim_number": self.claim_number,
            "status": self.status,
            "next_step": QUEUE_NEXT_STEP[queue],
            "outstanding": missing if partial else [],
            "documents_to_send": ev["missing_documents"],
            "say_to_caller": "Give the claim number one character at a time, the next step, and the documents to send.",
        }

    # ------------------------------------------------------------ validation

    def _validate(self, key: str, value: Any) -> tuple[bool, Any]:
        ftype = FIELD_TYPES.get(key)
        if ftype is None:
            return False, "Unknown field."
        now = self.now()
        try:
            if ftype == "date":
                d = date.fromisoformat(str(value).strip()[:10])
                if d > now.date():
                    return False, "Date is in the future. Confirm with the caller."
                return True, d.isoformat()
            if ftype == "time":
                v = str(value).strip()
                if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                    return False, "Use 24-hour HH:MM."
                h, m = v.split(":")
                return True, f"{int(h):02d}:{m}"
            if ftype == "datetime":
                dt = datetime.fromisoformat(str(value).strip()).replace(tzinfo=None, second=0, microsecond=0)
                if dt > now + timedelta(minutes=5):
                    return False, "That time is in the future. Confirm with the caller."
                return True, dt.isoformat(timespec="minutes")
            if ftype == "bool":
                if isinstance(value, bool):
                    return True, value
                v = str(value).strip().lower()
                if v in _TRUE:
                    return True, True
                if v in _FALSE:
                    return True, False
                return False, "Expected yes or no."
            if ftype == "int":
                n = int(float(str(value).replace(",", "")))
                return (True, n) if n >= 0 else (False, "Must be zero or more.")
            if ftype == "money":
                n = float(re.sub(r"[^0-9.]", "", str(value)) or "x")
                return True, round(n, 2)
            if ftype == "enum":
                v = str(value).strip().lower().replace(" ", "_").replace("-", "_")
                if v not in ENUMS[key]:
                    return False, f"Must be one of: {', '.join(ENUMS[key])}"
                return True, v
            if ftype == "unit":
                if not self.policy:
                    return False, "Look up the policy first so the truck can be matched to the schedule. Ask again after."
                v = str(value).strip().upper()
                ids = [u["id"] for u in self.policy.get("units", [])]
                if v in ids or v == "UNLISTED":
                    return True, v
                listed = "; ".join(f"{u['id']} {u['description']}" for u in self.policy.get("units", []))
                return False, f"Not a unit id. Units on policy: {listed}. Use UNLISTED if it's not one of these."
            if ftype == "text":
                v = str(value).strip()
                return (True, v[:1500]) if v else (False, "Empty value.")
        except (ValueError, TypeError):
            return False, f"Could not parse a {ftype}."
        return False, "Unsupported field."

    # -------------------------------------------------------- requirements

    def _required(self) -> list[str]:
        f, tp = self.fields, self.third_party
        seq: list[str] = []
        if self.line == "trucking":
            seq = ["caller_ok", "loss_date", "loss_time", "location", "state", "injuries"]
            if f.get("injuries") is True:
                seq += ["injured_count", "injury_treated_away", "fatality"]
            if tp:
                seq += ["description", "claimant_vehicle", "insured_vehicle_description", "police_report"]
            else:
                seq += ["description", "accident_type", "third_party_vehicles", "unit_id", "driver_name",
                        "dispatch_status", "hazmat", "towed"]
                if f.get("towed") is True:
                    seq.append("tow_yard")
                seq += ["citation_issued", "cargo_involved"]
                if f.get("cargo_involved") is True or f.get("accident_type") in ("cargo_theft", "cargo_damage"):
                    seq += ["cargo_description", "cargo_value"]
                seq += ["police_report", "dashcam"]
        elif self.line == "cyber":
            seq = ["caller_ok", "incident_type", "discovered_at", "description"]
            it = f.get("incident_type")
            if it == "funds_transfer_fraud":
                seq += ["wire_amount", "wire_sent_at", "bank_notified", "wire_international"]
            if it == "ransomware":
                seq += ["ransom_demanded", "ransom_paid", "contacted_attacker"]
            seq += ["systems_affected", "operations_down"]
            if it in ("ransomware", "data_breach"):
                seq.append("pii_involved")
            if f.get("pii_involved") is True:
                seq.append("records_estimate")
            seq += ["vendor_engaged", "law_enforcement_notified"]
        elif self.line == "liability":
            seq = ["caller_ok", "matter_type", "claimant_name", "claim_received_date", "allegation", "description"]
            if f.get("matter_type") == "lawsuit":
                seq.append("served")
            if f.get("served") is True:
                seq.append("service_date")
            if f.get("allegation") in ("product_failure", "ai_output"):
                seq.append("alleged_act_date")
            seq += ["responded_to_claimant", "counsel_retained"]
        return list(dict.fromkeys(seq))

    def _missing(self) -> list[str]:
        if not self.line:
            return []
        req = self._required()
        if not self.policy:
            at = req.index("description") + 1 if "description" in req else len(req)
            req = req[:at] + ["policy"] + req[at:]
        return [k for k in req if k == "policy" or k not in self.fields]

    def _next_q(self, missing: list[str]) -> dict:
        if not missing:
            return {"field": "read_back_and_confirm",
                    "ask": "Read back the key facts, ask if anything is wrong, then submit with caller_confirmed true."}
        if missing[0] == "policy":
            ask = ("Which trucking company was it? If you saw a USDOT number on the door, that helps too."
                   if self.third_party else "Let me pull up your policy so this goes to the right team. Do you have the policy number handy?")
            return {"field": "policy", "ask": ask, "tool": "lookup_policy"}
        return {"field": missing[0], "ask": ASK.get(missing[0], missing[0])}

    # ------------------------------------------------------------ evaluation

    def _loss_dt(self) -> datetime | None:
        f = self.fields
        if "loss_date" in f and "loss_time" in f:
            return datetime.combine(date.fromisoformat(f["loss_date"]), time.fromisoformat(f["loss_time"]))
        return None

    def _evaluate(self) -> dict:
        flags: list[dict] = []
        actions: list[dict] = []
        f, now = self.fields, self.now()
        p = self.policy or {}

        def flag(code, sev, reason, internal=False):
            flags.append({"code": code, "severity": sev, "reason": reason, "source": "rule", "internal": internal})

        def act(aid, text, deadline=None, priority="high"):
            actions.append({"id": aid, "text": text, "deadline": deadline.isoformat(timespec="minutes") if deadline else None,
                            "priority": priority})

        loss_date = date.fromisoformat(f["loss_date"]) if "loss_date" in f else None

        # ---------- caller wellbeing (every claim type)
        if f.get("caller_ok") is False:
            note = f.get("wellbeing_note")
            flag("caller_unwell", "medium", "Caller said they are not okay" + (f": {note}." if note else ".")
                 + " Handle with care on callback.")
            if self._physical_risk():
                act("caller_care", "If you're hurt, please get looked at first. Call 911 if it's serious, and we can finish this "
                    "afterwards. The claim isn't going anywhere.", None, "critical")

        # ---------- policy-level coverage checks
        if self.caller and self.caller["role"] in ("policyholder_contact", "driver") and self.policy and not self.authorized:
            flag("unverified_contact", "low", f"{self.caller['name']} is not a listed contact or driver. Limit what is disclosed.")
        if self.third_party:
            flag("third_party_claimant", "medium", "Caller is a third party. Do not disclose coverage or limits; no liability statements.")
        if loss_date and self.policy:
            if p.get("status") == "cancelled" and loss_date >= p["cancel_date"]:
                flag("coverage_question", "high", f"Policy cancelled effective {p['cancel_date']}; loss date {loss_date}.")
            claims_made_line = self.line in ("cyber", "liability")
            if not (p["term_start"] <= loss_date < p["term_end"]):
                what = "Claim first received" if self.line == "liability" else "Discovered" if self.line == "cyber" else "Loss date"
                flag("coverage_question", "high",
                     f"{what} {loss_date} is outside the policy period {p['term_start']} to {p['term_end']}"
                     + (" (claims-made trigger)." if claims_made_line else "."))
            if (loss_date - p["term_start"]).days <= 30 and loss_date >= p["term_start"]:
                flag("early_loss", "low", "Loss within 30 days of inception. Routine review.", internal=True)
            for c in EXISTING_CLAIMS:
                if c["policy_key"] == self.policy_key and c["line"] == self.line and c["loss_date"] == f["loss_date"]:
                    flag("possible_duplicate", "medium", f"Matches existing claim {c['claim_number']} ({c['summary']}).")
        if p and p.get("prior_claims_12mo", 0) >= 2:
            flag("claim_frequency", "low", "Two or more claims in the last 12 months.", internal=True)

        triggers = self._coverage_triggers()
        if self.line and triggers and not any(t["on_policy"] for t in triggers):
            flag("no_matching_coverage", "medium",
                 f"Likely coverage ({', '.join(t['coverage'] for t in triggers)}) is not on this policy. Record notice; adjuster decides.")

        # ---------- line-specific rules
        dot = None
        if self.line == "trucking":
            dot = self._dot_testing()
            self._trucking_rules(f, p, now, flag, act, dot)
        elif self.line == "cyber":
            self._cyber_rules(f, p, now, flag, act)
        elif self.line == "liability":
            self._liability_rules(f, p, now, flag, act)

        if loss_date and self.line == "trucking" and (now.date() - loss_date).days > 30:
            flag("late_notice", "medium", f"Reported {(now.date() - loss_date).days} days after the loss.")

        severity, drivers = self._severity()
        routing = self._routing(severity, flags + self.sticky_flags)
        return {
            "flags": flags,
            "actions": actions,
            "severity": severity,
            "severity_drivers": drivers,
            "coverage_triggers": triggers,
            "missing_documents": self._documents(dot),
            "routing": routing,
            "dot_testing": dot,
        }

    def _trucking_rules(self, f, p, now, flag, act, dot):
        tp = self.third_party
        if f.get("fatality") is True:
            flag("fatality", "high", "Fatality reported.")
        if f.get("injuries") is True:
            flag("bodily_injury", "high", "Injury reported" + (" by third-party claimant." if tp else "."))
        if f.get("hazmat") is True:
            flag("hazmat", "high", "Hazmat involved.")
            act("hazmat", "If anything is leaking, please stay well back, and make sure 911 knows there's hazmat on the truck.", None, "critical")
        if tp:
            if f.get("injuries") is True:
                act("tp_medical", "Please do get that looked at properly. Your health comes first, the paperwork can wait.", None, "medium")
            return

        unit = f.get("unit_id")
        if unit == "UNLISTED":
            flag("coverage_question", "medium", "Unit not on the schedule. Check newly acquired vehicle provisions.")
        if self.policy and "driver_name" in f and not any(name_matches(f["driver_name"], d) for d in p.get("drivers", [])):
            flag("unlisted_driver", "medium", f"Driver {f['driver_name']} is not on the driver list.")
        if f.get("dispatch_status") in ("bobtail", "personal_use"):
            flag("coverage_question", "medium",
                 "Truck was not under dispatch. Check whether non-trucking or bobtail liability applies.")
        cargo_limit = p.get("coverages", {}).get("cargo", {}).get("limit")
        if f.get("cargo_value") and cargo_limit and f["cargo_value"] > cargo_limit:
            flag("coverage_question", "medium", f"Reported cargo value ${f['cargo_value']:,.0f} exceeds cargo limit ${cargo_limit:,}.")
        if f.get("accident_type") == "cargo_theft":
            flag("theft_review", "medium", "Cargo theft. Specialist review.")
            if f.get("police_report") is False:
                act("police", "Cargo theft needs a police report. Please file one and get the report number.", None, "high")
                if (f.get("cargo_value") or 0) >= 50_000:
                    flag("siu_review", "medium", "High-value cargo theft with no police report.", internal=True)

        if dot and dot.get("required"):
            ab, dd = dot.get("alcohol_by"), dot.get("drug_by")
            if ab:
                ab_dt, dd_dt = datetime.fromisoformat(ab), datetime.fromisoformat(dd)
                if now > ab_dt:
                    flag("dot_window_missed", "high", "8-hour alcohol test window has passed. Fleet must document why.")
                else:
                    act("dot_alcohol", f"Based on what you've told me, this one needs a DOT post-accident test. The alcohol test has to happen by "
                        f"{_fmt(ab_dt)}, and no alcohol at all until then, not even one.", ab_dt, "critical")
                if now <= dd_dt:
                    act("dot_drug", f"The drug test has a longer window, by {_fmt(dd_dt)}.", dd_dt, "high")
                act("dot_notify", "Your safety manager should hear about this tonight so they can book the test.", None, "high")
        if f.get("dashcam") is True:
            act("dashcam", "Can you save that dashcam clip as soon as you can? Most cameras record over old footage, and that video can really help you.", None, "high")
        if (f.get("third_party_vehicles") or 0) > 0 or f.get("injuries") is True:
            act("preserve", "Please hold off on any repairs until we've pulled the truck's data. It protects you.", None, "high")
            act("no_fault", "Swapping information is fine, but try not to talk about who was at fault, and don't sign anything.", None, "medium")
        if f.get("towed") is True:
            act("tow", "Tow yards charge storage by the day, so we'll move quickly on getting the truck out.", None, "medium")

    def _cyber_rules(self, f, p, now, flag, act):
        it = f.get("incident_type")
        cyber = p.get("coverages", {}).get("cyber", {})
        vendor = (f.get("vendor_engaged") or "").strip()
        non_panel = bool(vendor) and vendor.lower() not in _FALSE and not any(vendor.lower() == v.lower() for v in PANEL_VENDORS)
        if non_panel:
            flag("non_panel_vendor", "medium", f"Insured engaged {vendor}, which is not an approved vendor. Costs need consent.")
        if "discovered_at" in f:
            hrs = (now - datetime.fromisoformat(f["discovered_at"])).total_seconds() / 3600
            if hrs > 72:
                flag("late_notice", "medium", f"Reported about {hrs:.0f} hours after discovery.")

        if it == "ransomware":
            if f.get("ransom_paid") is True:
                flag("ransom_paid", "high", "Ransom paid before consent. Needs sanctions screening and coverage review.")
            elif f.get("contacted_attacker") is True:
                flag("attacker_contact", "medium", "Insured has communicated with the threat actor.")
            if f.get("ransom_paid") is not True:
                act("no_ransom", "Please don't pay or reply to them yet. That decision needs our sign-off, and we'll help you think it through.", None, "critical")
            act("isolate", "Unplug the affected machines from the network, but don't wipe them. They hold the evidence of what happened.", None, "critical")
        if it == "funds_transfer_fraud":
            sub = cyber.get("sublimits", {}).get("social_engineering")
            if f.get("wire_amount") and sub and f["wire_amount"] > sub:
                flag("coverage_question", "medium",
                     f"Wire ${f['wire_amount']:,.0f} exceeds the ${sub:,} social engineering sublimit.")
            if "wire_sent_at" in f:
                sent = datetime.fromisoformat(f["wire_sent_at"])
                window = sent + timedelta(hours=72)
                if now <= window:
                    if f.get("bank_notified") is not True:
                        act("wire_recall", f"Right after we hang up, call your bank's fraud line and ask them to recall the wire. "
                            f"The best chance of getting it back is in the first 72 hours, so before {_fmt(window)}.", window, "critical")
                    act("ic3", "Then file at ic3.gov today. That's how the FBI can try to freeze the money on the other end.",
                        window, "critical")
                else:
                    flag("recall_window_passed", "medium", "Transfer is more than 72 hours old. Recovery less likely.")
                    act("ic3", "Still file at ic3.gov with the wire details, even though it's been more than 72 hours.", None, "high")
            act("mailbox", "Change that email password and turn on two-factor, but keep the fake emails. We'll need them.", None, "high")
        if it in ("ransomware", "data_breach", "funds_transfer_fraud"):
            if not vendor or vendor.lower() in _FALSE:
                act("vendors", "You don't need to find a security firm yourself. Your breach coach will bring in approved ones, so those costs are taken care of.", None, "high")
            elif non_panel:
                act("vendor_pause", f"Please pause any new work with {vendor} until the breach coach confirms it's approved, "
                    "so those costs stay reimbursable.", None, "high")
        if f.get("pii_involved") is True:
            act("notify", "Breach counsel will help you figure out who needs to be notified and by when, so hold off on sending notices yourself.", None, "high")
            act("comms", "Same with any public statement. Talk to counsel first.", None, "medium")

    def _liability_rules(self, f, p, now, flag, act):
        cov = p.get("coverages", {})
        eo = cov.get("tech_eo", {})
        if f.get("alleged_act_date") and eo.get("retro_date"):
            if date.fromisoformat(f["alleged_act_date"]) < eo["retro_date"]:
                flag("coverage_question", "high",
                     f"Alleged act {f['alleged_act_date']} predates the Tech E&O retroactive date {eo['retro_date']}.")
        if f.get("claim_received_date"):
            days = (now.date() - date.fromisoformat(f["claim_received_date"])).days
            if days > 60:
                flag("late_notice", "high", f"Claim received {days} days ago. Claims-made reporting conditions may apply.")
        if f.get("responded_to_claimant") is True:
            flag("insured_communicated", "low", "Insured has already communicated with the claimant.")
        if f.get("counsel_retained") is True:
            flag("counsel_before_consent", "medium", "Defense counsel retained before consent. Check consent-to-defend terms.")

        if f.get("matter_type") == "potential_claim":
            act("noc", "Good call letting us know early. We'll log it as a notice of circumstances, so if it turns into a claim later, it's tied to this policy.", None, "medium")
        else:
            act("dont_respond", "From here, please don't reply to them or negotiate. Send anything you get to us and we'll handle it.", None, "high")
        if f.get("served") is True and f.get("service_date"):
            d = datetime.combine(date.fromisoformat(f["service_date"]), time(17, 0)) + timedelta(days=20)
            act("answer", "Lawsuits can have response deadlines as short as 20 to 30 days from service, so please send the complaint "
                "today. Defense counsel will confirm the exact date.", d, "critical")
        if f.get("counsel_retained") is False:
            act("consent", "Please check with us before hiring a lawyer or agreeing to anything. That keeps defense costs covered.", None, "high")

    # ----------------------------------------------------- derived outputs

    def _dot_testing(self) -> dict | None:
        """49 CFR 382.303: test after a fatality, or a citation plus injury treated away or a tow-away."""
        f = self.fields
        if self.third_party:
            return None
        fatal = f.get("fatality") is True
        qualifying = f.get("injury_treated_away") is True or f.get("towed") is True
        required = fatal or (f.get("citation_issued") is True and qualifying)
        known = "citation_issued" in f and "towed" in f and "injuries" in f
        if not required:
            return {"required": False} if known else None
        out = {"required": True, "basis": "fatality" if fatal else "citation plus injury treated away or tow-away"}
        ldt = self._loss_dt()
        if ldt:
            out |= {
                "alcohol_by": (ldt + timedelta(hours=8)).isoformat(timespec="minutes"),
                "alcohol_document_after": (ldt + timedelta(hours=2)).isoformat(timespec="minutes"),
                "drug_by": (ldt + timedelta(hours=32)).isoformat(timespec="minutes"),
            }
        return out

    def _coverage_triggers(self) -> list[dict]:
        if not self.line or not self.policy:
            return []
        f, cov = self.fields, self.policy.get("coverages", {})
        names: list[str] = []
        if self.line == "trucking":
            at = f.get("accident_type")
            if self.third_party or (f.get("third_party_vehicles") or 0) > 0:
                names.append("auto_liability")
            if not self.third_party and at not in ("cargo_theft", "cargo_damage"):
                names.append("physical_damage")
            if f.get("cargo_involved") is True or at in ("cargo_theft", "cargo_damage"):
                names.append("cargo")
        elif self.line == "cyber":
            names.append("cyber")
        elif self.line == "liability":
            mapping = {
                "product_failure": ["tech_eo"], "ai_output": ["tech_eo"], "data_privacy": ["cyber", "tech_eo"],
                "employment": ["epli"], "governance": ["dno"], "bodily_injury_property": ["cgl"],
            }
            names = mapping.get(f.get("allegation"), [])
            if f.get("matter_type") == "regulatory_inquiry" and "dno" not in names:
                names.append("dno")
        return [{"coverage": n, "on_policy": n in cov} for n in dict.fromkeys(names)]

    def _severity(self) -> tuple[int, list[str]]:
        f, s, why = self.fields, 1, []

        def bump(n, reason):
            nonlocal s
            s += n
            why.append(reason)

        if self.line == "trucking":
            if f.get("caller_ok") is False and f.get("injuries") is not True:
                bump(1, "caller hurt or unwell")
            if self.third_party:
                bump(1, "third-party claimant")
            if (f.get("third_party_vehicles") or 0) > 0:
                bump(1, "other vehicle involved")
            if f.get("injuries") is True:
                bump(1, "injury reported")
            if f.get("injury_treated_away") is True:
                bump(1, "injury treated away from scene")
            if f.get("accident_type") in ("rollover", "jackknife", "fire", "head_on"):
                bump(1, f"{f['accident_type'].replace('_', ' ')}")
            if (f.get("cargo_value") or 0) >= 50_000 and (f.get("cargo_involved") or f.get("accident_type") in ("cargo_theft", "cargo_damage")):
                bump(1, "high-value cargo")
            if f.get("hazmat") is True:
                s = max(s, 4); why.append("hazmat")
            if f.get("fatality") is True:
                s = 5; why.append("fatality")
        elif self.line == "cyber":
            it = f.get("incident_type")
            if it in ("ransomware", "data_breach", "funds_transfer_fraud"):
                bump(2, it.replace("_", " "))
            if f.get("operations_down") is True:
                bump(1, "operations down")
            if f.get("pii_involved") is True and (f.get("records_estimate") or 0) >= 10_000:
                bump(1, "10k+ personal records")
            if (f.get("wire_amount") or 0) >= 100_000:
                bump(1, "wire over $100k")
            if f.get("ransom_paid") is True:
                s = 5; why.append("ransom already paid")
        elif self.line == "liability":
            mt = f.get("matter_type")
            base = {"potential_claim": 0, "demand_letter": 1, "lawsuit": 2, "regulatory_inquiry": 3}.get(mt, 0)
            if base:
                bump(base, mt.replace("_", " "))
            if f.get("served") is True:
                bump(1, "served")
            if (f.get("amount_demanded") or 0) >= 500_000:
                bump(1, "demand of $500k or more")
        return max(1, min(5, s)), why

    def _routing(self, severity: int, flags: list[dict]) -> dict:
        f, codes = self.fields, {x["code"] for x in flags}
        if self.line == "trucking":
            if f.get("fatality") is True or f.get("hazmat") is True or (
                f.get("injuries") is True and (self.third_party or (f.get("third_party_vehicles") or 0) > 0)
            ):
                queue = "rapid_response"
            elif severity <= 2 and not codes - {"unverified_contact"}:
                queue = "fast_track"
            else:
                queue = "desk_adjuster"
        elif self.line == "cyber":
            queue = "breach_response"
        elif self.line == "liability":
            queue = "litigation" if f.get("matter_type") in ("lawsuit", "regulatory_inquiry") else "liability_desk"
        else:
            queue = "desk_adjuster"
        secondary = []
        if codes & {"coverage_question", "no_matching_coverage", "late_notice", "possible_duplicate"}:
            secondary.append("coverage_review")
        if codes & {"siu_review", "early_loss", "claim_frequency"}:
            secondary.append("siu_review")
        if "transfer_requested" in codes:
            secondary.append("live_transfer")
        return {"queue": queue, "secondary": secondary}

    def _documents(self, dot) -> list[str]:
        f, docs = self.fields, []
        if self.line == "trucking":
            docs.append("Photos of every vehicle and the scene")
            if f.get("police_report") is True:
                docs.append("Police report" + (f" #{f['police_report_number']}" if f.get("police_report_number") else ""))
            if self.third_party:
                docs += ["Repair estimate"] + (["Medical bills and records"] if f.get("injuries") else [])
            else:
                docs.append("Driver's license and DOT medical card")
                if f.get("dashcam") is True:
                    docs.append("Dashcam clip, original file")
                if (f.get("third_party_vehicles") or 0) > 0 or f.get("injuries") is True:
                    docs.append("ECM and ELD download")
                if f.get("towed") is True:
                    docs.append("Tow receipt and yard address")
                if f.get("cargo_involved") is True or f.get("accident_type") in ("cargo_theft", "cargo_damage"):
                    docs.append("Bill of lading")
                if dot and dot.get("required"):
                    docs.append("Post-accident test results or documentation of why not tested")
        elif self.line == "cyber":
            it = f.get("incident_type")
            if it == "ransomware":
                docs += ["Screenshot of the ransom note", "List of affected systems", "Status of backups"]
            if it == "funds_transfer_fraud":
                docs += ["Wire confirmation", "Bank recall reference number", "IC3 complaint number",
                         "The fraudulent email thread, kept in the original mailbox"]
            if it == "data_breach":
                docs += ["Description of the data involved", "Timeline of discovery"]
            v = f.get("vendor_engaged")
            if v and v.lower() not in _FALSE:
                docs.append(f"Engagement letter and invoices from {v}")
        elif self.line == "liability":
            docs += ["The demand letter or complaint", "Contract with the claimant, if any", "Related emails and messages"]
            if f.get("served") is True:
                docs.append("Summons and proof of service")
        return docs

    # ----------------------------------------------------------- views

    _PHYSICAL = re.compile(r"hurt|pain|injur|bleed|dizz|hospital|ambulance|sore|broke|concuss|faint|head", re.I)

    def _physical_risk(self) -> bool:
        role = (self.caller or {}).get("role")
        return (
            self.line == "trucking"
            or role in ("driver", "third_party_claimant")
            or bool(self._PHYSICAL.search(self.fields.get("wellbeing_note") or ""))
        )

    def _care_guidance(self) -> str | None:
        """Tells the agent what the person needs, not what words to say."""
        if self.fields.get("caller_ok") is not False or self.care_given:
            return None
        self.care_given = True
        note = self.fields.get("wellbeing_note") or "said they are not okay"
        g = (f"The caller is not okay ({note}). Respond to that before anything else, the way a kind, experienced person would: "
             "reflect back the specific thing they told you in your own words, take some weight off them if it fits "
             "(this happens to careful people, calling quickly was the right move), and give them control over the pace "
             "(offer a moment, or a callback). One or two sentences, no stock apology, then continue more slowly.")
        if self._physical_risk():
            g += " They may be physically hurt, so check whether they need medical help first."
        return g

    def _new_say_now(self, ev: dict) -> list[str]:
        new = [a for a in ev["actions"] if a["id"] not in self.announced]
        self.announced |= {a["id"] for a in new}
        return [a["text"] for a in sorted(new, key=lambda a: a["priority"] != "critical")]

    def _agent_feedback(self, ev: dict) -> dict:
        """What the model sees. Internal (SIU) flags are never sent to the voice agent."""
        say_now = self._new_say_now(ev)
        missing = self._missing()
        care = self._care_guidance()
        return ({"care_guidance": care} if care else {}) | {
            "say_now": say_now,
            "next_question": self._next_q(missing),
            "missing_count": len(missing),
            "flags": sorted({x["code"] for x in ev["flags"] + self.sticky_flags if not x.get("internal")}),
        }

    def _policy_view_for_agent(self) -> dict:
        p = self.policy
        if self.third_party:
            return {"insured": p["insured"], "note": "Third-party caller. Do not share policy number, coverage, or limits."}
        base = {"insured": p["insured"], "policy_number": p["policy_number"], "program": p["program"]}
        if not self.authorized:
            return base | {"note": "Caller is not a listed contact. Take the report but do not discuss coverage details."}
        base["coverages"] = list(p["coverages"])
        if p.get("units"):
            base["units"] = [f"{u['id']}: {u['description']}" for u in p["units"]]
        return base

    def _sticky(self, code: str, severity: str, reason: str, source: str) -> None:
        self.sticky_flags.append({"code": code, "severity": severity, "reason": reason, "source": source, "internal": False})

    def _brief(self, ev: dict) -> str:
        f = self.fields
        p = self.policy or {"insured": "Insured not yet matched", "policy_number": "policy pending", "units": []}
        head = f"{p['insured']} ({p['policy_number']}) | {self.line or 'line not set'} | severity {ev['severity']}/5 | {ev['routing']['queue']}"
        parts = []
        if self.caller:
            parts.append(f"Reported by {self.caller['name']} ({self.caller['role'].replace('_', ' ')}).")
            if "caller_ok" in f:
                parts.append("Caller wellbeing: " + ("okay" if f["caller_ok"] else "NOT okay")
                             + (f" ({f['wellbeing_note']})" if f.get("wellbeing_note") else "") + ".")
        if self.line == "trucking":
            when = " ".join(x for x in (f.get("loss_date"), f.get("loss_time")) if x)
            where = ", ".join(x for x in (f.get("location"), f.get("state")) if x)
            if when or where:
                parts.append(f"{f.get('accident_type', 'Loss').replace('_', ' ').capitalize()} {when} at {where}.".replace("  ", " "))
            if f.get("unit_id"):
                u = next((u["description"] for u in p.get("units", []) if u["id"] == f["unit_id"]), "unlisted unit")
                parts.append(f"Unit {f['unit_id']} ({u}), driver {f.get('driver_name', 'unknown')}, {f.get('dispatch_status', '').replace('_', ' ')}.")
            if f.get("third_party_vehicles"):
                parts.append(f"{f['third_party_vehicles']} other vehicle(s).")
            if f.get("injuries") is True:
                parts.append(f"{f.get('injured_count', '?')} injured" + ("; transported." if f.get("injury_treated_away") else "."))
            if f.get("citation_issued") is True:
                parts.append("Citation issued.")
            d = ev.get("dot_testing")
            if d and d.get("required") and d.get("alcohol_by"):
                parts.append(f"DOT testing required: alcohol by {d['alcohol_by'][11:]}, drug by {d['drug_by'].replace('T', ' ')}.")
        elif self.line == "cyber":
            if f.get("incident_type"):
                parts.append(f"{f['incident_type'].replace('_', ' ').capitalize()} discovered {f.get('discovered_at', '?').replace('T', ' ')}.")
            if f.get("wire_amount"):
                parts.append(f"Wire ${f['wire_amount']:,.0f} sent {f.get('wire_sent_at', '?').replace('T', ' ')}; bank notified: {_yn(f.get('bank_notified'))}.")
            if f.get("ransom_amount"):
                parts.append(f"Demand {f['ransom_amount']}; paid: {_yn(f.get('ransom_paid'))}.")
            if f.get("records_estimate"):
                parts.append(f"About {f['records_estimate']:,} personal records possibly exposed.")
        elif self.line == "liability":
            if f.get("matter_type"):
                parts.append(f"{f['matter_type'].replace('_', ' ').capitalize()} from {f.get('claimant_name', '?')} received {f.get('claim_received_date', '?')}.")
            if f.get("allegation"):
                parts.append(f"Allegation: {f['allegation'].replace('_', ' ')}.")
            if f.get("amount_demanded"):
                parts.append(f"Demand ${f['amount_demanded']:,.0f}.")
        if f.get("description"):
            parts.append(f"Caller: \"{f['description']}\"")
        cov = [t["coverage"] + ("" if t["on_policy"] else " (not on policy)") for t in ev["coverage_triggers"]]
        if cov:
            parts.append("Potential coverage: " + ", ".join(cov) + ".")
        return head + "\n" + " ".join(parts)

    def _service_brief(self) -> str:
        who = f"{self.caller['name']} ({self.caller['role'].replace('_', ' ')})" if self.caller else "Caller not identified"
        reason = (self.call_reason or "not set").replace("_", " ")
        lines = [f"Call reason: {reason}" + (f" | {self.call_summary}" if self.call_summary else ""), f"Caller: {who}"]
        if self.looked_up_claim:
            r = CLAIM_REGISTRY[self.looked_up_claim]
            lines.append(f"Claim {r['claim_number']} ({r['insured']}, {r['line']}, loss {r['loss_date']}): {r['status']}, {r['team']}.")
            for n in self.notes_added:
                lines.append(f"New note from {n['by']} at {n['at']}: \"{n['note']}\"")
        if self.service_request:
            sr = self.service_request
            lines.append(f"Service request {sr['number']} [{sr['topic'].replace('_', ' ')}]: {sr['details']}"
                         + (f" Deadline: {sr['deadline']}." if sr.get("deadline") else ""))
        return "\n".join(lines)

    def snapshot(self) -> dict:
        ev = self._evaluate()
        has_line = bool(self.line)
        return {
            "claim_number": self.claim_number,
            "status": self.status,
            "caller": self.caller,
            "caller_wellbeing": {"ok": self.fields.get("caller_ok"), "note": self.fields.get("wellbeing_note")},
            "policy": {"insured": self.policy["insured"], "policy_number": self.policy["policy_number"],
                       "program": self.policy["program"], "authorized_caller": self.authorized} if self.policy else None,
            "line": self.line,
            "fields": dict(self.fields),
            "missing_required": self._missing(),
            "flags": sorted(ev["flags"] + self.sticky_flags, key=lambda x: -SEV[x["severity"]]),
            "urgent_actions": ev["actions"],
            "severity": ev["severity"] if has_line else None,
            "severity_drivers": ev["severity_drivers"] if has_line else [],
            "coverage_triggers": ev["coverage_triggers"],
            "missing_documents": ev["missing_documents"],
            "routing": ev["routing"] if has_line else None,
            "dot_testing": ev["dot_testing"],
            "adjuster_brief": self._brief(ev) if has_line else (self._service_brief() if self.call_reason else ""),
            "call_reason": self.call_reason,
            "service_request": self.service_request,
            "existing_claim": (CLAIM_REGISTRY[self.looked_up_claim] | {"notes_this_call": self.notes_added})
                              if self.looked_up_claim else None,
            "now": self.now().isoformat(timespec="minutes"),
            "tool_calls": len(self.events),
        }
