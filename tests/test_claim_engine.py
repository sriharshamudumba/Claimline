from datetime import datetime

import pytest

from app import scenarios
from app.claim_engine import ClaimSession
from app.prompt import TOOLS

NOW = datetime(2026, 11, 13, 14, 0)


def run(scn: dict) -> tuple[ClaimSession, list[dict]]:
    s = new_loss(ClaimSession("t", now=NOW))
    results = []
    for step in scn["steps"]:
        if "tool" in step:
            results.append(s.dispatch(step["tool"], step["args"]))
    return s, results


# ---------------------------------------------------------------- scenarios

@pytest.mark.parametrize("scn", scenarios.load(NOW), ids=lambda x: x["id"])
def test_scenario_runs_clean_and_routes_correctly(scn):
    s, results = run(scn)
    errors = [r for r in results if "error" in r or r.get("rejected")]
    assert not errors, errors
    snap = s.snapshot()
    exp = scn["expect"]
    assert snap["status"] == exp["status"]
    if "queue" in exp:
        assert snap["routing"]["queue"] == exp["queue"]
    if "call_reason" in exp:
        assert snap["call_reason"] == exp["call_reason"]
    if exp.get("service_request"):
        assert snap["service_request"]["number"].startswith("SR-")
    if exp.get("note_added"):
        assert snap["existing_claim"]["notes_this_call"]
    if "severity" in exp:
        assert snap["severity"] == exp["severity"], snap["severity_drivers"]
    codes = {f["code"] for f in snap["flags"]}
    assert set(exp.get("flags", [])) <= codes, codes
    if exp.get("dot_required"):
        assert snap["dot_testing"]["required"] is True


# ------------------------------------------------------------ helpers

def new_loss(s):
    s.set_call_reason("new_claim")
    return s


def trucking_session(**caller):
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller(caller.get("name", "Marcus Hill"), caller.get("role", "driver"), caller_ok=caller.get("ok", True))
    assert s.lookup_policy("XYZ Freight", policy_number="ct-20417")["matched"]
    s.open_claim("trucking")
    return s


def startup_session(line, name="Priya Shah", policy="TS-88120", biz="ABC Labs"):
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller(name, "policyholder_contact", caller_ok=True)
    assert s.lookup_policy(biz, policy_number=policy)["matched"]
    s.open_claim(line)
    return s


# ------------------------------------------------------------ flow guards

def test_tools_enforce_order():
    s = new_loss(ClaimSession("t", now=NOW))
    assert s.lookup_policy("XYZ")["error"] == "caller_not_identified"
    assert s.open_claim("trucking")["error"] == "caller_not_identified"
    s.identify_caller("Marcus Hill", "driver", caller_ok=True)
    assert s.update_claim({"injuries": True})["error"] == "claim_not_open"


def test_policy_number_must_match_business_name():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Marcus Hill", "driver", caller_ok=True)
    assert not s.lookup_policy("ABC Labs", policy_number="CT-20417")["matched"]


def test_three_failed_lookups_escalate():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("X", "policyholder_contact", caller_ok=True)
    for _ in range(3):
        s.lookup_policy("Nobody Inc", policy_number="ZZ-1")
    assert s.lookup_policy("XYZ", policy_number="CT-20417")["error"] == "max_attempts_reached"
    assert any(f["code"] == "policy_not_located" for f in s.snapshot()["flags"])


def test_unlisted_caller_gets_limited_disclosure():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Random Person", "policyholder_contact", caller_ok=True)
    r = s.lookup_policy("XYZ Freight", policy_number="CT-20417")
    assert "coverages" not in r and "note" in r


def test_third_party_never_sees_policy_details():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Rosa Delgado", "third_party_claimant", caller_ok=True)
    r = s.lookup_policy("XYZ Freight", usdot_number="9900417")
    assert "policy_number" not in r and "coverages" not in r


def test_submit_requires_confirmation_and_completeness():
    s = trucking_session()
    assert s.submit_claim(True)["error"] == "incomplete_claim"
    assert s.submit_claim(True, partial=True)["error"] == "partial_needs_basics"


def test_prompt_injection_cannot_change_state():
    s = trucking_session()
    r = s.dispatch("approve_claim", {"amount": 1_000_000})
    assert "error" in r and s.status == "intake"


# ------------------------------------------------------------ trucking rules

def test_dot_testing_clock_and_say_now():
    s = trucking_session()
    s.update_claim({"loss_date": "2026-11-13", "loss_time": "11:30", "injuries": False, "third_party_vehicles": 1})
    r = s.update_claim({"towed": True, "citation_issued": True})
    assert any("alcohol test has to happen by 7:30 PM" in x for x in r["say_now"])
    dot = s.snapshot()["dot_testing"]
    assert dot["alcohol_by"] == "2026-11-13T19:30" and dot["drug_by"] == "2026-11-14T19:30"


def test_no_dot_testing_without_citation():
    s = trucking_session()
    s.update_claim({"loss_date": "2026-11-13", "loss_time": "11:30", "injuries": False, "towed": True, "citation_issued": False})
    assert s.snapshot()["dot_testing"] == {"required": False}


def test_missed_alcohol_window_flagged():
    s = trucking_session()
    s.update_claim({"loss_date": "2026-11-12", "loss_time": "20:00", "injuries": True, "injury_treated_away": True,
                    "citation_issued": True, "towed": False})
    assert "dot_window_missed" in {f["code"] for f in s.snapshot()["flags"]}


def test_future_time_rejected():
    s = trucking_session()
    r = s.update_claim({"loss_date": "2026-11-13", "loss_time": "22:40"})
    assert "loss_time" in r["rejected"]


def test_bobtail_unlisted_driver_and_unit_flag_coverage():
    s = trucking_session()
    s.update_claim({"dispatch_status": "bobtail", "driver_name": "Jake Doe", "unit_id": "UNLISTED"})
    codes = [f["code"] for f in s.snapshot()["flags"]]
    assert codes.count("coverage_question") == 2 and "unlisted_driver" in codes


def test_bad_unit_rejected_with_options():
    s = trucking_session()
    r = s.update_claim({"unit_id": "T9"})
    assert "Units on policy" in r["rejected"]["unit_id"]


def test_cargo_over_limit_and_theft_without_report():
    s = trucking_session()
    s.update_claim({"accident_type": "cargo_theft", "cargo_value": 140000, "police_report": False})
    snap = s.snapshot()
    codes = {f["code"] for f in snap["flags"]}
    assert {"coverage_question", "theft_review", "siu_review"} <= codes


def test_internal_flags_hidden_from_agent():
    s = trucking_session()
    r = s.update_claim({"accident_type": "cargo_theft", "cargo_value": 140000, "police_report": False})
    assert "siu_review" not in r["flags"]


def test_duplicate_claim_detected():
    s = trucking_session()
    s.update_claim({"loss_date": "2026-09-02"})
    assert any("CLM-260902-4417" in f["reason"] for f in s.snapshot()["flags"])


def test_cancelled_policy_flagged():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Sam Reyes", "driver", caller_ok=True)
    s.lookup_policy("DEF Haul", policy_number="CT-17003")
    s.open_claim("trucking")
    s.update_claim({"loss_date": "2026-11-10"})
    assert any("cancelled" in f["reason"] for f in s.snapshot()["flags"])


def test_simple_pd_only_is_fast_tracked():
    s = trucking_session()
    s.update_claim({"accident_type": "backing", "third_party_vehicles": 0, "injuries": False})
    assert s.snapshot()["routing"]["queue"] == "fast_track"


def test_corrections_clear_stale_flags():
    s = trucking_session()
    s.update_claim({"injuries": True})
    assert "bodily_injury" in s.update_claim({"injured_count": 1})["flags"]
    assert "bodily_injury" not in s.update_claim({"injuries": False})["flags"]


# --------------------------------------------------------------- cyber rules

def test_ransom_paid_is_max_severity():
    s = startup_session("cyber")
    s.update_claim({"incident_type": "ransomware", "ransom_paid": True})
    snap = s.snapshot()
    assert snap["severity"] == 5 and "ransom_paid" in {f["code"] for f in snap["flags"]}


def test_wire_window_passed():
    s = startup_session("cyber")
    r = s.update_claim({"incident_type": "funds_transfer_fraud", "wire_sent_at": "2026-11-09T09:00", "wire_amount": 50000})
    assert "recall_window_passed" in r["flags"]


def test_panel_vendor_is_fine():
    s = startup_session("cyber")
    s.update_claim({"incident_type": "ransomware", "vendor_engaged": "RST IR"})
    assert "non_panel_vendor" not in {f["code"] for f in s.snapshot()["flags"]}


def test_late_cyber_notice():
    s = startup_session("cyber")
    s.update_claim({"incident_type": "data_breach", "discovered_at": "2026-11-05T10:00"})
    assert "late_notice" in {f["code"] for f in s.snapshot()["flags"]}


# ----------------------------------------------------------- liability rules

def test_retro_date_flag():
    s = startup_session("liability")
    s.update_claim({"allegation": "product_failure", "alleged_act_date": "2024-12-01"})
    assert any("retroactive date" in f["reason"] for f in s.snapshot()["flags"])


def test_claims_made_trigger_outside_period():
    s = startup_session("liability")
    s.update_claim({"claim_received_date": "2026-03-20"})
    codes = {f["code"] for f in s.snapshot()["flags"]}
    assert {"coverage_question", "late_notice"} <= codes


def test_served_lawsuit_creates_answer_deadline_and_litigation_queue():
    s = startup_session("liability")
    r = s.update_claim({"matter_type": "lawsuit", "served": True, "service_date": "2026-11-10"})
    assert any("20 to 30 days" in x for x in r["say_now"])
    assert s.snapshot()["routing"]["queue"] == "litigation"


def test_employment_claim_without_epli_flags_no_coverage():
    s = startup_session("liability", name="Leo Park", policy="TS-91002", biz="PQR AI")
    s.update_claim({"allegation": "employment"})
    assert "no_matching_coverage" in {f["code"] for f in s.snapshot()["flags"]}


def test_potential_claim_logged_as_notice_of_circumstances():
    s = startup_session("liability")
    r = s.update_claim({"matter_type": "potential_claim"})
    assert any("notice of circumstances" in x for x in r["say_now"])


# ------------------------------------------------------------- schema sanity

def test_tool_schema_covers_every_field():
    from app.claim_engine import FIELD_TYPES
    props = next(t for t in TOOLS if t["name"] == "update_claim")["parameters"]["properties"]["fields"]["properties"]
    assert set(props) == set(FIELD_TYPES)


# ------------------------------------------------------------ caller wellbeing

def test_cannot_start_without_asking_if_caller_is_okay():
    s = new_loss(ClaimSession("t", now=NOW))
    r = s.identify_caller("Marcus Hill", "driver")
    assert r["error"] == "wellbeing_not_checked" and s.caller is None


def test_upset_cyber_caller_gets_empathy_not_911():
    s = new_loss(ClaimSession("t", now=NOW))
    r = s.identify_caller("Dan Wu", "policyholder_contact", caller_ok=False, wellbeing_note="very upset, blames himself")
    assert r["say_now"] == []  # no medical script for an emotional situation
    assert "blames himself" in r["care_guidance"] and "medical" not in r["care_guidance"]
    assert any(f["code"] == "caller_unwell" and "very upset" in f["reason"] for f in s.snapshot()["flags"])


def test_hurt_caller_gets_medical_check():
    s = new_loss(ClaimSession("t", now=NOW))
    r = s.identify_caller("Rosa Delgado", "third_party_claimant", caller_ok=False, wellbeing_note="shoulder hurts")
    assert any("911" in x for x in r["say_now"]) and "medical help" in r["care_guidance"]


def test_injury_words_trigger_medical_check_even_for_startup_caller():
    s = new_loss(ClaimSession("t", now=NOW))
    r = s.identify_caller("Priya Shah", "policyholder_contact", caller_ok=False, wellbeing_note="chest pain, panicking")
    assert any("911" in x for x in r["say_now"])


def test_care_guidance_given_once():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Dan Wu", "policyholder_contact", caller_ok=False)
    s.lookup_policy("ABC Labs", policy_number="TS-88120")
    assert "care_guidance" not in s.open_claim("cyber")


def test_okay_caller_gets_no_care_message():
    s = new_loss(ClaimSession("t", now=NOW))
    assert s.identify_caller("Priya Shah", "policyholder_contact", caller_ok="yes")["say_now"] == []


def test_wellbeing_can_change_mid_call():
    s = trucking_session()
    r = s.update_claim({"caller_ok": False, "wellbeing_note": "feeling dizzy"})
    assert any("911" in x for x in r["say_now"])
    assert "caller hurt or unwell" in s.snapshot()["severity_drivers"]


def test_wellbeing_is_first_question_on_every_line():
    for line in ("trucking", "cyber", "liability"):
        s = new_loss(ClaimSession("t", now=NOW))
        s.identify_caller("Priya Shah", "policyholder_contact", caller_ok=True)
        s.fields.pop("caller_ok")
        s.lookup_policy("ABC Labs", policy_number="TS-88120")
        assert s.open_claim(line)["next_question"]["field"] == "caller_ok"


def test_partial_submit_requires_wellbeing():
    s = trucking_session()
    s.fields.pop("caller_ok")
    s.update_claim({"loss_date": "2026-11-13", "description": "x"})
    assert "caller_ok" in s.submit_claim(True, partial=True)["missing"]


# ------------------------------------------------------------ story before policy

def test_caller_can_tell_the_story_before_the_policy_lookup():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Marcus Hill", "driver", caller_ok=True)
    assert s.open_claim("trucking")["opened"]
    r = s.update_claim({"loss_date": "2026-11-13", "loss_time": "12:00", "location": "I-80 WB, Reno", "state": "NV",
                        "injuries": False, "description": "Rear-ended a sedan"})
    assert not r["rejected"]
    assert r["next_question"]["field"] == "policy"  # policy is asked right after the story
    m = s.lookup_policy("XYZ Freight", policy_number="CT-20417")
    assert m["matched"] and m["next_question"]["field"] == "accident_type"


def test_unit_needs_policy_first():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Marcus Hill", "driver", caller_ok=True)
    s.open_claim("trucking")
    assert "policy first" in s.update_claim({"unit_id": "T1"})["rejected"]["unit_id"]


def test_caller_who_must_leave_can_submit_without_policy():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Marcus Hill", "driver", caller_ok=True)
    s.open_claim("trucking")
    s.update_claim({"loss_date": "2026-11-13", "description": "Crash, trooper waiting"})
    assert s.submit_claim(True)["error"] == "incomplete_claim"
    r = s.submit_claim(True, partial=True)
    assert r["submitted"] and "policy" in r["outstanding"]
    assert "policy_not_located" in {f["code"] for f in s.snapshot()["flags"]}


def test_third_party_policy_question_asks_for_company_and_dot():
    s = new_loss(ClaimSession("t", now=NOW))
    s.identify_caller("Rosa Delgado", "third_party_claimant", caller_ok=True)
    s.open_claim("trucking")
    r = s.update_claim({"loss_date": "2026-11-13", "loss_time": "12:00", "location": "I-580", "state": "CA",
                        "injuries": False, "description": "Sideswiped"})
    assert "USDOT" in r["next_question"]["ask"]


# ------------------------------------------------------------ call reason routing

def test_must_ask_why_they_are_calling_first():
    s = ClaimSession("t", now=NOW)
    assert s.identify_caller("Marcus Hill", "driver", caller_ok=True)["error"] == "reason_not_set"


def test_wellbeing_only_required_for_new_losses():
    s = ClaimSession("t", now=NOW)
    s.set_call_reason("billing")
    assert s.identify_caller("Priya Shah", "policyholder_contact")["ok"]
    s2 = new_loss(ClaimSession("t2", now=NOW))
    assert s2.identify_caller("Priya Shah", "policyholder_contact")["error"] == "wellbeing_not_checked"


def test_status_call_can_turn_into_new_loss_and_asks_wellbeing_first():
    s = ClaimSession("t", now=NOW)
    s.set_call_reason("existing_claim")
    s.identify_caller("Marcus Hill", "driver")
    r = s.open_claim("trucking")
    assert s.call_reason == "new_claim" and r["next_question"]["field"] == "caller_ok"


def test_claim_lookup_needs_matching_business():
    s = ClaimSession("t", now=NOW)
    s.set_call_reason("existing_claim")
    s.identify_caller("Someone", "policyholder_contact")
    assert not s.lookup_claim("CLM-260902-4417", "ABC Labs")["found"]
    assert s.lookup_claim("clm 260902 4417", "XYZ Freight")["found"]


def test_note_requires_verified_claim():
    s = ClaimSession("t", now=NOW)
    s.set_call_reason("existing_claim")
    s.identify_caller("Marcus Hill", "driver")
    assert s.add_claim_note("CLM-260902-4417", "x")["error"] == "claim_not_verified"


def test_claim_filed_on_one_call_is_findable_on_the_next():
    s, _ = run(next(x for x in scenarios.load(NOW) if x["id"] == "wire_fraud"))
    number = s.snapshot()["claim_number"]
    s2 = ClaimSession("t2", now=NOW)
    s2.set_call_reason("existing_claim")
    s2.identify_caller("Dan Wu", "policyholder_contact")
    r = s2.lookup_claim(number, "ABC Labs")
    assert r["found"] and r["line"] == "cyber"


def test_service_request_created():
    s = ClaimSession("t", now=NOW)
    s.set_call_reason("certificate_request")
    s.identify_caller("Priya Shah", "policyholder_contact")
    r = s.create_service_request("certificate_request", "COI for JKL", deadline="Friday")
    assert r["request_number"].startswith("SR-") and "Friday" in r["say_to_caller"]
