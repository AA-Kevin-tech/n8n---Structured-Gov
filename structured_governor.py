"""
structured_governor.py

Structured Governor (single-file reference implementation)

What it is:
- A governance layer that enforces grounding in facts and constraints,
  exploration of approaches and trade-offs, concrete resolution with options,
  self-reflection for rhetorical drift, and final approval gating.
- Includes retry + PATCH MODE prompts to force minimal edits rather than full rewrites.
- Intended for auditability and low-rhetoric discipline.

What it is NOT:
- It does not make an LLM "fully autonomous."
- It does not transfer responsibility away from humans.
- It is a validation/orchestration layer that can be used in safe contexts.

Dependencies:
  pip install pydantic

Optional:
  pip install openai  (if you want an OpenAI adapter; see stub)
  pip install flask   (if you want the HTTP server for n8n)

Run:
  python structured_governor.py
  echo '{"user_message":"What are the diet feedouts for cattle?"}' | python structured_governor.py --stdin
  python structured_governor.py --serve   # HTTP API for n8n
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError


# ============================================================
# 1) Pydantic Schema (StructuredGovernor v1.0)
# ============================================================

class Meta(BaseModel):
    contract_name: str = Field("StructuredGovernor")
    version: str = Field("1.0")
    enforcement_mode: str = Field("hard_gate")  # hard_gate | soft_gate
    phase_visibility: str = Field("required")   # required | optional
    notes: Optional[str] = None


class Input(BaseModel):
    user_message: str
    context: Optional[Dict[str, Any]] = None


class Grounding(BaseModel):
    task: str
    known_facts: List[str]
    constraints: List[str]
    unknowns: List[str] = []
    output_contract: str
    verification_hooks: List[str]


class FramePressure(BaseModel):
    detected: bool
    notes: str


class TimeboxPlan(BaseModel):
    slow_points: List[str] = []
    fast_points: List[str] = []
    stop_condition: str


class Exploration(BaseModel):
    approach_options: List[str]
    chosen_path: str
    tradeoffs: List[str]
    alternative_interpretations: List[str]
    frame_pressure: FramePressure
    timebox_plan: TimeboxPlan


class Option(BaseModel):
    option: str
    pros: List[str]
    cons: List[str]


class Resolution(BaseModel):
    answer: str
    options: List[Option]
    next_actions: List[str]


class DriftFlags(BaseModel):
    moralizing: int = 0
    persuasion: int = 0
    authority_substitution: int = 0
    fluency_masking: int = 0
    premature_optimization: int = 0
    identity_projection: int = 0


class Reflection(BaseModel):
    drift_flags: DriftFlags
    drift_score: float
    clarity_edits: List[str] = []


class Approval(BaseModel):
    constraints_passed: bool
    structure_score: float
    approved: bool
    reasons: List[str]


class PhaseDurations(BaseModel):
    grounding: int = 0
    exploration: int = 0
    resolution: int = 0
    reflection: int = 0
    approval: int = 0


class Telemetry(BaseModel):
    phase_durations_ms: PhaseDurations
    retry_count: int = 0
    notes: Optional[str] = None


class StructuredGovernorOutput(BaseModel):
    meta: Meta
    input: Input
    grounding: Grounding
    exploration: Exploration
    resolution: Resolution
    reflection: Reflection
    approval: Approval
    telemetry: Telemetry


# ============================================================
# 2) Rules Layer (thresholds, scores, gates)
# ============================================================

DEFAULTS = {
    "min_structure_score": 0.8,
    "max_drift_score": 0.2,
    "max_retries": 2,
    "min_known_facts": 1,
    "min_constraints": 1,
    "min_verification_hooks": 1,
    "min_approach_options": 1,
    "min_tradeoffs": 1,
    "min_alternative_interpretations": 1,
    "min_options": 1,
    "min_next_actions": 1,
    "max_next_actions": 10,
}

DRIFT_WEIGHTS = {
    "moralizing": 0.25,
    "persuasion": 0.25,
    "authority_substitution": 0.15,
    "fluency_masking": 0.15,
    "premature_optimization": 0.10,
    "identity_projection": 0.10,
}


class RuleFailure(BaseModel):
    error_code: str
    message: str


class EnforcementResult(BaseModel):
    passed: bool
    failures: List[RuleFailure] = []
    computed_structure_score: float
    computed_drift_score: float
    should_retry: bool


def compute_structure_score(o: StructuredGovernorOutput) -> float:
    # 5 equally weighted components = 0.2 each
    score = 0.0
    score += 0.2 if (o.grounding.task or "").strip() else 0.0
    score += 0.2 if len(o.grounding.known_facts) >= 1 else 0.0
    score += 0.2 if len(o.grounding.constraints) >= 1 else 0.0
    score += 0.2 if (o.grounding.output_contract or "").strip() else 0.0
    score += 0.2 if len(o.grounding.verification_hooks) >= 1 else 0.0
    return round(score, 4)


def _clamp(v: int, lo: int = 0, hi: int = 4) -> int:
    return max(lo, min(hi, v))


def compute_drift_score(o: StructuredGovernorOutput) -> float:
    flags = o.reflection.drift_flags.model_dump()
    s = 0.0
    for k, w in DRIFT_WEIGHTS.items():
        c = _clamp(int(flags.get(k, 0)))
        s += w * (c / 4.0)
    return round(min(1.0, s), 4)


def validate_rules(payload: dict, thresholds: dict = DEFAULTS) -> Tuple[Optional[StructuredGovernorOutput], EnforcementResult]:
    failures: List[RuleFailure] = []

    # Schema parse
    try:
        o = StructuredGovernorOutput(**payload)
    except ValidationError as e:
        return None, EnforcementResult(
            passed=False,
            failures=[RuleFailure(error_code="SCHEMA_INVALID", message=str(e))],
            computed_structure_score=0.0,
            computed_drift_score=1.0,
            should_retry=True
        )

    # Compute scores
    sscore = compute_structure_score(o)
    dscore = compute_drift_score(o)

    # Gates
    if (
        len(o.grounding.known_facts) < thresholds["min_known_facts"]
        or len(o.grounding.constraints) < thresholds["min_constraints"]
        or len(o.grounding.verification_hooks) < thresholds["min_verification_hooks"]
        or not o.grounding.task.strip()
        or not o.grounding.output_contract.strip()
    ):
        failures.append(RuleFailure(
            error_code="GROUNDING_INCOMPLETE",
            message="Grounding incomplete: require task, >=1 known_facts, >=1 constraints, output_contract, >=1 verification_hooks."
        ))

    if (
        len(o.exploration.approach_options) < thresholds["min_approach_options"]
        or not o.exploration.chosen_path.strip()
        or len(o.exploration.tradeoffs) < thresholds["min_tradeoffs"]
        or len(o.exploration.alternative_interpretations) < thresholds["min_alternative_interpretations"]
        or not o.exploration.timebox_plan.stop_condition.strip()
    ):
        failures.append(RuleFailure(
            error_code="EXPLORATION_INCOMPLETE",
            message="Exploration incomplete: require approach_options, chosen_path, tradeoffs, alternative_interpretations, timebox stop_condition."
        ))

    if (
        not o.resolution.answer.strip()
        or len(o.resolution.options) < thresholds["min_options"]
        or len(o.resolution.next_actions) < thresholds["min_next_actions"]
        or len(o.resolution.next_actions) > thresholds["max_next_actions"]
    ):
        failures.append(RuleFailure(
            error_code="RESOLUTION_INCOMPLETE",
            message="Resolution incomplete: require answer, >=1 option (pros/cons), and next_actions length 1–10."
        ))

    if dscore > thresholds["max_drift_score"]:
        failures.append(RuleFailure(
            error_code="DRIFT_TOO_HIGH",
            message=f"Reflection drift too high: {dscore} (max {thresholds['max_drift_score']}). Reduce moralizing/persuasion/authority/fluency masking."
        ))

    if (not o.approval.constraints_passed) or (not o.approval.approved) or (sscore < thresholds["min_structure_score"]):
        failures.append(RuleFailure(
            error_code="APPROVAL_NOT_GRANTED",
            message="Approval failed: requires constraints_passed=true, approved=true, and computed structure_score >= threshold."
        ))

    passed = len(failures) == 0
    should_retry = any(f.error_code in {"GROUNDING_INCOMPLETE", "EXPLORATION_INCOMPLETE", "RESOLUTION_INCOMPLETE", "DRIFT_TOO_HIGH", "SCHEMA_INVALID"} for f in failures) and not any(
        f.error_code == "APPROVAL_NOT_GRANTED" for f in failures
    )

    return o, EnforcementResult(
        passed=passed,
        failures=failures,
        computed_structure_score=sscore,
        computed_drift_score=dscore,
        should_retry=should_retry
    )


# ============================================================
# 3) Prompts: system + generation + PATCH MODE retry
# ============================================================

def system_prompt() -> str:
    return (
        "You are operating under the StructuredGovernor contract v1.0.\n\n"
        "NON-NEGOTIABLES:\n"
        "- Output MUST be valid JSON ONLY. No markdown. No commentary. No backticks.\n"
        "- Output MUST conform to the StructuredGovernor schema v1.0 exactly.\n"
        "- Do not add extra keys. Do not omit required keys.\n"
        "- Maintain phase order: meta, input, grounding, exploration, resolution, reflection, approval, telemetry.\n"
        "- No interpretation or answer until grounding is complete.\n"
        "- Drift must be detected, flagged, and scored in reflection.\n"
        "- No final output unless approval.approved is true.\n\n"
        "STYLE:\n"
        "- Use plain, concrete language.\n"
        "- No moralizing. No persuasion. No authority substitution.\n"
        "- Do not claim certainty where unknowns exist.\n\n"
        "If you cannot comply, output JSON conforming to schema and explain failure inside approval.reasons with approved=false."
    )


def generation_prompt(user_message: str, context: Optional[Dict[str, Any]] = None) -> str:
    return (
        "TASK: Produce a single JSON object conforming to StructuredGovernor schema v1.0.\n\n"
        f"USER_MESSAGE:\n{user_message}\n\n"
        f"CONTEXT (optional):\n{json.dumps(context or {}, ensure_ascii=False)}\n\n"
        "REQUIREMENTS:\n"
        "1) grounding: task, known_facts, constraints, unknowns, output_contract, verification_hooks\n"
        "2) exploration: approach_options, chosen_path, tradeoffs, alternative_interpretations, frame_pressure{detected,notes}, timebox_plan{slow_points,fast_points,stop_condition}\n"
        "3) resolution: answer, options[{option,pros[],cons[]}], next_actions length 1–10\n"
        "4) reflection: drift_flags ints, drift_score 0–1, clarity_edits[]\n"
        "5) approval: constraints_passed bool, structure_score 0–1 estimate, approved bool, reasons[]\n"
        "6) telemetry: phase_durations_ms.* ints, retry_count int\n\n"
        "IMPORTANT:\n"
        "- Estimate approval.structure_score; validator computes its own.\n"
        "- Set approved=true only if constraints are satisfied and drift is low.\n"
        "- Keep drift low: avoid should/must/obviously, flattery, trust me, appeals to authority.\n\n"
        "OUTPUT: JSON ONLY."
    )


def patch_mode_retry_prompt(
    user_message: str,
    previous_json: str,
    failures: List[Dict[str, str]],
    edit_targets: Optional[List[str]] = None
) -> str:
    failures_text = "\n".join([f"- {f['error_code']}: {f['message']}" for f in failures])
    edit_targets_block = ""
    if edit_targets:
        edit_targets_block = (
            "\nEDIT_TARGETS (JSON pointer-like paths; change ONLY these unless strictly required):\n"
            + "\n".join([f"- {t}" for t in edit_targets])
            + "\n"
        )

    return (
        "PATCH MODE ENABLED.\n\n"
        "GOAL:\n"
        "- Apply the smallest possible changes to PREVIOUS_JSON to satisfy VALIDATION_FAILURES.\n"
        "- Preserve all unaffected fields verbatim.\n"
        "- Do NOT improve content unless required by a failure.\n\n"
        "OUTPUT RULE:\n"
        "- Output must be FULL JSON conforming to StructuredGovernor schema v1.0.\n"
        "- JSON only.\n\n"
        f"USER_MESSAGE:\n{user_message}\n\n"
        f"PREVIOUS_JSON:\n{previous_json}\n\n"
        f"VALIDATION_FAILURES:\n{failures_text}\n"
        f"{edit_targets_block}\n"
        "PATCH INSTRUCTIONS:\n"
        "1) Treat PREVIOUS_JSON as the base.\n"
        "2) Modify ONLY minimal fields needed to clear failures.\n"
        "3) Do not introduce new facts.\n"
        "4) If DRIFT_TOO_HIGH: remove drift phrases; reduce drift_flags; add a note in reflection.clarity_edits.\n"
        "5) Update telemetry.retry_count += 1.\n\n"
        "OUTPUT: JSON ONLY."
    )


def derive_edit_targets(failures: List[Dict[str, str]]) -> List[str]:
    targets: List[str] = []
    codes = {f["error_code"] for f in failures}

    if "SCHEMA_INVALID" in codes:
        return ["/"]

    if "GROUNDING_INCOMPLETE" in codes:
        targets += [
            "/grounding/task",
            "/grounding/known_facts",
            "/grounding/constraints",
            "/grounding/output_contract",
            "/grounding/verification_hooks",
        ]
    if "EXPLORATION_INCOMPLETE" in codes:
        targets += [
            "/exploration/approach_options",
            "/exploration/chosen_path",
            "/exploration/tradeoffs",
            "/exploration/alternative_interpretations",
            "/exploration/timebox_plan/stop_condition",
        ]
    if "RESOLUTION_INCOMPLETE" in codes:
        targets += [
            "/resolution/answer",
            "/resolution/options",
            "/resolution/next_actions",
        ]
    if "DRIFT_TOO_HIGH" in codes:
        targets += [
            "/resolution/answer",
            "/reflection/drift_flags",
            "/reflection/drift_score",
            "/reflection/clarity_edits",
        ]
    if "APPROVAL_NOT_GRANTED" in codes:
        targets += [
            "/approval/constraints_passed",
            "/approval/approved",
            "/approval/reasons",
        ]

    targets.append("/telemetry/retry_count")
    return sorted(set(targets))


# ============================================================
# 4) Patch log (diff) for "minimal edits" auditing
# ============================================================

def _diff_paths(a: Any, b: Any, path: str = "") -> List[str]:
    paths: List[str] = []
    if type(a) != type(b):
        return [path or "/"]
    if isinstance(a, dict):
        for k in sorted(set(a.keys()).union(b.keys())):
            p = f"{path}/{k}" if path else f"/{k}"
            if k not in a or k not in b:
                paths.append(p)
            else:
                paths.extend(_diff_paths(a[k], b[k], p))
        return paths
    if isinstance(a, list):
        if len(a) != len(b):
            return [path or "/"]
        for i, (ai, bi) in enumerate(zip(a, b)):
            p = f"{path}/{i}" if path else f"/{i}"
            paths.extend(_diff_paths(ai, bi, p))
        return paths
    if a != b:
        paths.append(path or "/")
    return paths


def patch_log(prev_json_str: str, new_json_str: str) -> List[str]:
    try:
        prev = json.loads(prev_json_str)
        new = json.loads(new_json_str)
    except Exception:
        return ["/"]
    return _diff_paths(prev, new)


# ============================================================
# 5) Governor Runtime Loop (LLM → validate → patch retry)
# ============================================================

@dataclass
class AttemptRecord:
    attempt_index: int
    passed: bool
    computed_structure_score: float
    computed_drift_score: float
    failures: List[Dict[str, str]]
    changed_paths: List[str]
    candidate_json: str


def run_governed(
    user_message: str,
    llm_call: Callable[[str, str], str],  # (system, prompt) -> JSON string
    context: Optional[Dict[str, Any]] = None,
    max_retries: int = DEFAULTS["max_retries"],
) -> Tuple[str, List[AttemptRecord]]:
    sys_p = system_prompt()
    prompt = generation_prompt(user_message, context)

    records: List[AttemptRecord] = []
    prev_json_str: Optional[str] = None

    for attempt in range(max_retries + 1):
        candidate = llm_call(sys_p, prompt)

        # Parse JSON
        try:
            payload = json.loads(candidate)
        except Exception as e:
            failures = [{"error_code": "SCHEMA_INVALID", "message": f"Non-JSON output or parse error: {e}"}]
            rec = AttemptRecord(
                attempt_index=attempt,
                passed=False,
                computed_structure_score=0.0,
                computed_drift_score=1.0,
                failures=failures,
                changed_paths=["/"],
                candidate_json=candidate
            )
            records.append(rec)

            # retry
            prev_json_str = candidate
            prompt = patch_mode_retry_prompt(user_message, prev_json_str, failures, edit_targets=["/"])
            continue

        # Validate
        _, res = validate_rules(payload)
        passed = res.passed
        failures = [f.model_dump() for f in res.failures]
        changed = patch_log(prev_json_str, candidate) if prev_json_str else []

        rec = AttemptRecord(
            attempt_index=attempt,
            passed=passed,
            computed_structure_score=res.computed_structure_score,
            computed_drift_score=res.computed_drift_score,
            failures=failures,
            changed_paths=changed,
            candidate_json=candidate
        )
        records.append(rec)

        if passed:
            return candidate, records

        # Stop if out of retries
        if attempt >= max_retries:
            return candidate, records

        # Patch-mode retry
        prev_json_str = candidate
        edit_targets = derive_edit_targets(failures)
        prompt = patch_mode_retry_prompt(user_message, prev_json_str, failures, edit_targets=edit_targets)

    return records[-1].candidate_json, records


# ============================================================
# 6) LLM Adapters
# ============================================================

def mock_llm(system: str, prompt: str) -> str:
    """
    Demo behavior:
    - First response: intentionally fails (missing fields + drift).
    - Second response (patch mode): fixes only required fields.
    """
    if "PATCH MODE ENABLED" not in prompt:
        return json.dumps({
            "meta": {
                "contract_name": "StructuredGovernor",
                "version": "1.0",
                "enforcement_mode": "hard_gate",
                "phase_visibility": "required"
            },
            "input": {"user_message": "demo", "context": {}},
            "grounding": {
                "task": "Answer user",
                "known_facts": ["User wants a governed response."],
                "constraints": ["Return JSON only."],
                "unknowns": [],
                "output_contract": "",
                "verification_hooks": ["Validate schema."]
            },
            "exploration": {
                "approach_options": ["Respond quickly."],
                "chosen_path": "Respond quickly.",
                "tradeoffs": ["Fast"],
                "alternative_interpretations": [],
                "frame_pressure": {"detected": False, "notes": ""},
                "timebox_plan": {"slow_points": [], "fast_points": [], "stop_condition": ""}
            },
            "resolution": {
                "answer": "Great question, you should obviously do this now.",
                "options": [{"option": "A", "pros": ["x"], "cons": ["y"]}],
                "next_actions": ["Run it."]
            },
            "reflection": {
                "drift_flags": {
                    "moralizing": 1, "persuasion": 1, "authority_substitution": 0,
                    "fluency_masking": 0, "premature_optimization": 0, "identity_projection": 0
                },
                "drift_score": 0.6,
                "clarity_edits": []
            },
            "approval": {
                "constraints_passed": False,
                "structure_score": 0.4,
                "approved": False,
                "reasons": ["First draft."]
            },
            "telemetry": {
                "phase_durations_ms": {
                    "grounding": 1, "exploration": 1, "resolution": 1, "reflection": 1, "approval": 1
                },
                "retry_count": 0
            }
        }, indent=2)

    # Patch mode: parse the PREVIOUS_JSON from the prompt and minimally fix it
    prev_start = prompt.find("PREVIOUS_JSON:\n") + len("PREVIOUS_JSON:\n")
    prev_end = prompt.find("\n\nVALIDATION_FAILURES:")
    prev = prompt[prev_start:prev_end]
    base = json.loads(prev)

    # Minimal required fixes
    base["grounding"]["output_contract"] = "Provide governed analysis with clear structure, low drift, and verifiable next actions."
    base["exploration"]["alternative_interpretations"] = ["User wants a working governance layer, not persuasion."]
    base["exploration"]["timebox_plan"]["stop_condition"] = "If validator fails after max retries, halt and report failures."
    base["resolution"]["answer"] = "Implemented a governed runtime loop with validation, patch-mode retries, and drift control."
    base["reflection"]["drift_flags"]["moralizing"] = 0
    base["reflection"]["drift_flags"]["persuasion"] = 0
    base["reflection"]["drift_score"] = 0.0
    base["reflection"]["clarity_edits"] = ["Removed persuasion language; added missing required fields; updated Approval gate fields."]
    base["approval"]["constraints_passed"] = True
    base["approval"]["approved"] = True
    base["approval"]["reasons"] = ["Patched required fields only; reduced drift below threshold; passed approval gate."]
    base["telemetry"]["retry_count"] = int(base["telemetry"].get("retry_count", 0)) + 1

    return json.dumps(base, indent=2)


def _openai_llm(system: str, prompt: str) -> str:
    """Call OpenAI API. Requires OPENAI_API_KEY and openai package."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("pip install openai and set OPENAI_API_KEY to use OpenAI adapter")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    client = OpenAI(api_key=key)
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    # Strip markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def get_llm_call() -> Callable[[str, str], str]:
    """Use OpenAI if OPENAI_API_KEY is set; otherwise mock."""
    if os.environ.get("OPENAI_API_KEY"):
        return _openai_llm
    return mock_llm


# ============================================================
# 7) CLI and HTTP server for n8n
# ============================================================

def cli_main() -> None:
    """Read JSON from stdin or argv: { user_message, context? }. Run governor, print final JSON to stdout."""
    raw: Optional[str] = None
    if "--stdin" in sys.argv:
        raw = sys.stdin.read()
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        raw = sys.argv[1]
    else:
        # Demo
        raw = json.dumps({"user_message": "Demonstrate the Structured Governor output.", "context": {"demo": True}})

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"Invalid JSON: {e}\n")
        sys.exit(1)

    user_message = data.get("user_message") or data.get("message") or ""
    context = data.get("context") or {}

    if not user_message:
        sys.stderr.write("Missing user_message or message in input JSON.\n")
        sys.exit(1)

    llm = get_llm_call()
    final_json, _ = run_governed(user_message=user_message, llm_call=llm, context=context)
    print(final_json)


def create_app():
    """Flask app for HTTP API (optional). Run with: python -c \"from structured_governor import create_app; create_app().run(port=5050)\" or --serve."""
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        raise RuntimeError("pip install flask to use --serve")

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "contract": "StructuredGovernor", "version": "1.0"})

    @app.route("/govern", methods=["POST"])
    def govern():
        body = request.get_json(silent=True) or {}
        user_message = body.get("user_message") or body.get("message") or ""
        context = body.get("context") or {}
        if not user_message:
            return jsonify({"error": "Missing user_message or message"}), 400
        llm = get_llm_call()
        try:
            final_json, records = run_governed(user_message=user_message, llm_call=llm, context=context)
            out = json.loads(final_json)
            out["_telemetry"] = {
                "attempts": len(records),
                "passed": records[-1].passed if records else False,
            }
            return jsonify(out)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app


# ============================================================
# 8) Demo runner
# ============================================================

def main() -> None:
    if "--serve" in sys.argv:
        app = create_app()
        port = int(os.environ.get("PORT", 5050))
        app.run(host="0.0.0.0", port=port, debug=False)
        return

    if "--stdin" in sys.argv or (len(sys.argv) > 1 and sys.argv[1].strip().startswith("{")):
        cli_main()
        return

    # Inline demo
    user_message = "Demonstrate the Structured Governor output."
    final_json, attempts = run_governed(
        user_message=user_message,
        llm_call=mock_llm,
        context={"demo": True},
        max_retries=2
    )

    print("\n=== Attempts ===")
    for a in attempts:
        print(f"\nAttempt {a.attempt_index}: passed={a.passed} structure={a.computed_structure_score} drift={a.computed_drift_score}")
        if a.failures:
            for f in a.failures:
                print(" ", f["error_code"], "-", f["message"])
        if a.changed_paths:
            print(" Changed paths:", a.changed_paths)

    print("\n=== Final JSON ===")
    print(final_json)


if __name__ == "__main__":
    main()
