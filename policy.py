# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Literal

from openai import OpenAI

ApproachState = Literal["search", "approach", "greet", "wait_for_bottle", "photo_ready", "skip"]
InteractionPhase = Literal["find_guest", "confirm_bottle"]
Bearing = Literal["left", "center", "right", "unknown"]
RangeEstimate = Literal["far", "medium", "near", "inside_4m", "inside_1m", "unknown"]


@dataclass(frozen=True)
class FetchPolicyConfig:
    model: str = "gpt-5-mini"
    max_line_chars: int = 120


def _default_decision(reason: str) -> dict[str, Any]:
    return {
        "type": "decision",
        "state": "search",
        "candidate_found": False,
        "confidence": 0.0,
        "target": {
            "bearing": "unknown",
            "range": "unknown",
            "description": "",
            "free_hand_evidence": "",
            "busy_signals": [],
        },
        "safety": {
            "safe_to_approach": False,
            "stop_reason": reason,
        },
        "offer": {
            "drink": True,
            "photo": True,
        },
        "action": "search",
        "photo_ready": False,
        "bottle_visible": False,
        "framing": {
            "person_visible": False,
            "bottle_visible": False,
            "well_framed": False,
            "notes": "",
        },
        "line": "",
        "simulated_cmd_vel": {
            "linear_x": 0.0,
            "angular_z": 0.35,
            "duration_s": 0.8,
        },
        "notes": reason,
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("OpenAI response was not a JSON object")
    return parsed


def _as_bearing(value: Any) -> Bearing:
    if value in {"left", "center", "right", "unknown"}:
        return value
    return "unknown"


def _as_range(value: Any) -> RangeEstimate:
    if value in {"far", "medium", "near", "inside_4m", "inside_1m", "unknown"}:
        return value
    return "unknown"


def _cmd_for_target(bearing: Bearing, range_estimate: RangeEstimate) -> dict[str, float]:
    if range_estimate in {"inside_4m", "inside_1m"}:
        return {"linear_x": 0.0, "angular_z": 0.0, "duration_s": 0.0}

    angular_z = 0.0
    if bearing == "left":
        angular_z = 0.28
    elif bearing == "right":
        angular_z = -0.28
    elif bearing == "unknown":
        angular_z = 0.35

    linear_x = 0.0
    duration_s = 0.8
    if bearing in {"left", "center", "right"}:
        linear_x = 0.22 if range_estimate == "near" else 0.32
        duration_s = 0.9 if range_estimate == "near" else 1.2

    return {"linear_x": linear_x, "angular_z": angular_z, "duration_s": duration_s}


def _normalize_decision(
    raw: dict[str, Any],
    config: FetchPolicyConfig,
    interaction_phase: InteractionPhase = "find_guest",
) -> dict[str, Any]:
    candidate_found = bool(raw.get("candidate_found"))
    confidence = float(raw.get("confidence") or 0.0)
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    safety = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}
    offer = raw.get("offer") if isinstance(raw.get("offer"), dict) else {}
    framing = raw.get("framing") if isinstance(raw.get("framing"), dict) else {}

    bearing = _as_bearing(target.get("bearing"))
    range_estimate = _as_range(target.get("range"))
    safe_to_approach = bool(safety.get("safe_to_approach"))
    photo_ready = bool(raw.get("photo_ready") or framing.get("well_framed"))
    bottle_visible = bool(raw.get("bottle_visible") or framing.get("bottle_visible"))
    line = str(raw.get("line") or "").strip()

    if len(line) > config.max_line_chars:
        line = line[: config.max_line_chars].rsplit(" ", 1)[0].rstrip(".,;:") + "."

    if interaction_phase == "confirm_bottle":
        if photo_ready:
            state: ApproachState = "photo_ready"
        elif candidate_found:
            state = "wait_for_bottle"
        else:
            state = "search"
    elif not candidate_found:
        state: ApproachState = "search"
    elif not safe_to_approach:
        state = "skip"
    elif range_estimate in {"inside_4m", "inside_1m"}:
        state = "greet"
    else:
        state = "approach"

    if state not in {"greet", "photo_ready", "wait_for_bottle"}:
        line = ""

    cmd = _cmd_for_target(bearing, range_estimate) if state == "approach" else _default_decision("")["simulated_cmd_vel"]
    if state in {"greet", "wait_for_bottle", "photo_ready"}:
        cmd = {"linear_x": 0.0, "angular_z": 0.0, "duration_s": 0.0}

    action_by_state = {
        "search": "search",
        "approach": "approach",
        "greet": "wave_offer",
        "wait_for_bottle": "coach_photo",
        "photo_ready": "take_photo_dance",
        "skip": "skip",
    }

    return {
        "type": "decision",
        "state": state,
        "action": str(raw.get("action") or action_by_state[state]),
        "candidate_found": candidate_found,
        "confidence": max(0.0, min(1.0, confidence)),
        "target": {
            "bearing": bearing,
            "range": range_estimate,
            "description": str(target.get("description") or ""),
            "free_hand_evidence": str(target.get("free_hand_evidence") or ""),
            "lying_evidence": str(target.get("lying_evidence") or ""),
            "happy_evidence": str(target.get("happy_evidence") or ""),
            "busy_signals": list(target.get("busy_signals") or []),
        },
        "safety": {
            "safe_to_approach": safe_to_approach,
            "stop_reason": str(safety.get("stop_reason") or ""),
        },
        "offer": {
            "drink": bool(offer.get("drink", True)),
            "photo": bool(offer.get("photo", True)),
        },
        "photo_ready": photo_ready,
        "bottle_visible": bottle_visible,
        "framing": {
            "person_visible": bool(framing.get("person_visible", candidate_found)),
            "bottle_visible": bottle_visible,
            "well_framed": photo_ready,
            "notes": str(framing.get("notes") or ""),
        },
        "line": line,
        "simulated_cmd_vel": cmd,
        "notes": str(raw.get("notes") or ""),
    }


class FetchPolicy:
    """Vision policy for the Fetch interaction prototype."""

    def __init__(self, config: FetchPolicyConfig | None = None) -> None:
        self.config = config or FetchPolicyConfig()
        self._client = OpenAI()

    def analyze_frame(
        self,
        image_data_url: str,
        depth_hint: dict[str, Any] | None = None,
        interaction_phase: InteractionPhase = "find_guest",
    ) -> dict[str, Any]:
        if not os.getenv("OPENAI_API_KEY"):
            return _default_decision("OPENAI_API_KEY is not set")

        if not image_data_url.startswith("data:image/"):
            return _default_decision("Expected an image data URL")

        depth_note = "No depth estimate was provided."
        if depth_hint:
            depth_note = f"Depth hint from client: {json.dumps(depth_hint, sort_keys=True)}"

        if interaction_phase == "confirm_bottle":
            goal = """
Current phase: confirm_bottle.
- The dog has already waved and offered a Coke in exchange for an instant photo.
- Look for the same or primary person holding a Coke bottle, soda bottle, or clearly bottle-shaped drink from the dog's bag.
- The person and bottle must both be visible and well framed for a photo. Prefer head/upper body plus bottle in frame; reject if the bottle is cropped out, hidden, or too blurry.
- If ready, set photo_ready true, action "take_photo_dance", and line to a short direction ending with "cheese" or similar.
- If the person is visible but the bottle is missing or framing is bad, set candidate_found true, photo_ready false, action "coach_photo", and line to one short positioning instruction.
"""
        else:
            goal = """
Current phase: find_guest.
- Find one visible beachgoer who looks happy or relaxed and is lying down or reclining on a beach towel, sand, lounger, or beach chair.
- Reject people who are working, reading, holding a phone, holding a drink, using a laptop/tablet, actively eating, or otherwise busy.
- Prefer someone with hands free and an open posture.
- The dog should approach only if the path looks safe, then stop within a few meters.
- Generate the greeting only when the target is inside 4 meters.
- The greeting must wave, make a highly personalized joke from visible non-sensitive appearance/context, and offer a Coke/drink in exchange for an instant photo.
"""

        prompt = f"""
You are the behavior policy for a small robot dog Fetch.

Goal:
{goal}

Safety and privacy:
- Do not identify people or infer sensitive traits.
- Do not comment on race, ethnicity, gender, age, disability, body size, attractiveness, or medical state.
- Avoid insults and body-shaming. Keep humor based on visible, non-sensitive details: beach context, towel, hat, sunglasses, posture, lighting, colors, bags, or the heat.
- The line must be specific to this frame. Mention at least one concrete visible detail from the person or scene. Do not write generic lines like "Want a drink or photo?" or "You look thirsty."
- If the path looks blocked or the target is too close to another person or object, mark safe_to_approach false.

Return only JSON with this shape:
{{
  "candidate_found": true,
  "confidence": 0.0,
  "target": {{
    "bearing": "left|center|right|unknown",
    "range": "far|medium|near|inside_4m|inside_1m|unknown",
    "description": "short visible description",
    "free_hand_evidence": "why hands seem free",
    "lying_evidence": "why they appear lying down or reclining",
    "happy_evidence": "why they appear relaxed or happy",
    "busy_signals": ["phone", "drink", "book", "work", "none"]
  }},
  "safety": {{
    "safe_to_approach": true,
    "stop_reason": ""
  }},
  "offer": {{
    "drink": true,
    "photo": true
  }},
  "action": "search|approach|wave_offer|coach_photo|take_photo_dance|skip",
  "photo_ready": false,
  "bottle_visible": false,
  "framing": {{
    "person_visible": true,
    "bottle_visible": false,
    "well_framed": false,
    "notes": "short framing notes"
  }},
  "line": "one short dog line; it must reference visible context and the current phase",
  "notes": "short reasoning"
}}

Range rule:
- Use "inside_4m" when the camera is already close enough for greeting and a snapshot.
- Use "inside_1m" only when the person is clearly within 1 meter.
- Use "near" when the person is close but not clearly within 4 meters.
- If the depth hint center_median_m, center_p10_m, frame_median_m, or frame_p10_m is 4.0 or less and a visible available person is in that region, prefer "inside_4m".

{depth_note}
""".strip()

        response = self._client.chat.completions.create(
            model=self.config.model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        content = response.choices[0].message.content or "{}"
        parsed = _extract_json_object(content)
        return _normalize_decision(parsed, self.config, interaction_phase)
