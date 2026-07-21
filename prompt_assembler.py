"""Prompt assembler — 4-part image_prompt, video_prompt, negative_prompt per scene.

Four-part structure (spec §5):
  A. Project visual rules  (style, aspect ratio, quality)
  B. Asset lock            (per-scene characters with consistency lock text)
  C. Scene event           (event, interaction, environment, composition, camera)
  D. Negative constraints  (universal + user-selected)
"""
from __future__ import annotations

from storyboard_data import (
    SHOT_TYPE_EN, COLOR_TONE_EN,
    CAMERA_ANGLES_EN, CAMERA_MOVEMENTS_EN, CAMERA_SPEEDS_EN, CAMERA_STABILITY_EN,
    COMPOSITIONS_EN, ORIENTATIONS_EN,
    ENV_DYNAMICS_EN, EMOTIONS_EN,
    NEGATIVE_PROMPTS_EN, EXPRESSIONS_EN, GAZE_EN,
    TIMES_EN, WEATHER_EN, SCENE_LOCATIONS_EN,
    MODEL_MODES,
    EVENT_TYPES_EN, VISUAL_FOCUS_EN, TEXT_SAFE_AREA_EN, ASSET_TYPES_EN,
)
from prompts import PROMPT_STYLE_MAP

_NO_EVENT_WARNING = "【⚠ 此幕缺少可視化事件，生成結果可能只有角色站立】"
_UNIVERSAL_NEGATIVE = (
    "No unrequested wardrobe, color, scale, species, product-shape or material changes. "
    "No identity swaps, duplicated or missing required assets, extra people, "
    "distorted anatomy, unreadable hands, random text or watermark. "
    "Do not carry over a pose or gaze from another scene unless explicitly specified."
)


def assemble_image_prompt(scene, characters, scene_groups, production, model_mode: str = "通用") -> str:
    """Build a static image / keyframe prompt — 4-part structure."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    parts_a: list[str] = []
    parts_b: list[str] = []
    parts_c: list[str] = []

    # ── A. Project visual rules ───────────────────────────────────────────────
    style_desc = PROMPT_STYLE_MAP.get(getattr(scene, "style", "電影風"), "high quality digital art")
    parts_a.append(style_desc)

    tone_en = COLOR_TONE_EN.get(getattr(scene, "tone", "暖色"), "")
    if tone_en:
        parts_a.append(tone_en)

    ar = getattr(production, "aspect_ratio", "")
    if ar:
        parts_a.append(f"{ar} aspect ratio")

    gs = getattr(production, "global_style", "")
    if gs:
        parts_a.append(gs)

    if mode.get("image_tags", True):
        parts_a.append("masterpiece, best quality, highly detailed")

    # ── B. Asset lock ─────────────────────────────────────────────────────────
    lock_text = _build_asset_lock_text(scene, characters)
    if lock_text:
        parts_b.append(lock_text)

    # Relationship rules
    rr = getattr(production, "relationship_rules", "").strip()
    if rr:
        parts_b.append(rr)

    # Character descriptions
    char_desc = _build_char_image_desc(scene, characters)
    if char_desc:
        parts_b.append(char_desc)

    # ── C. Scene event ────────────────────────────────────────────────────────
    # Shot type
    shot_en = SHOT_TYPE_EN.get(getattr(scene, "shot_type", "中景"), "")
    if shot_en:
        parts_c.append(shot_en)

    # Environment
    env = _build_environment(scene, scene_groups, production)
    if env:
        parts_c.append(env)

    # Visual event
    event_raw = getattr(scene, "event", "").strip()
    if event_raw and event_raw != "（未設定）":
        evt_en = EVENT_TYPES_EN.get(event_raw, event_raw)
        if evt_en:
            parts_c.append(evt_en)

    # Interaction (free text — use verbatim)
    interaction = getattr(scene, "interaction", "").strip()
    if interaction:
        parts_c.append(interaction)

    # Visual focus
    focus_raw = getattr(scene, "visual_focus", "（無）")
    focus_en = VISUAL_FOCUS_EN.get(focus_raw, "")
    if focus_en:
        parts_c.append(focus_en)

    # Composition
    comp_raw = getattr(scene, "composition", "（無）")
    comp_en = COMPOSITIONS_EN.get(comp_raw, comp_raw if comp_raw not in ("（無）", "", "── 角色位置 ──") else "")
    if comp_en:
        parts_c.append(comp_en)

    # Orientation
    ori_en = ORIENTATIONS_EN.get(getattr(scene, "orientation", "（無）"), "")
    if ori_en:
        parts_c.append(ori_en)

    # Camera angle
    angle_en = CAMERA_ANGLES_EN.get(getattr(scene, "camera_angle", "平視"), "")
    if angle_en and angle_en != CAMERA_ANGLES_EN.get("平視"):
        parts_c.append(angle_en)

    # Text safe area
    safe_en = TEXT_SAFE_AREA_EN.get(getattr(scene, "text_safe_area", "無"), "")
    if safe_en:
        parts_c.append(safe_en)

    # Emotions
    emotions = getattr(scene, "emotions", [])
    if emotions:
        emo_en = [EMOTIONS_EN[e] for e in emotions if e in EMOTIONS_EN]
        if emo_en:
            parts_c.append(", ".join(emo_en))

    # ── Assemble with section labels ──────────────────────────────────────────
    sections: list[str] = []
    if parts_a:
        sections.append(", ".join(p for p in parts_a if p))
    if parts_b:
        sections.append(", ".join(p for p in parts_b if p))
    if parts_c:
        sections.append(", ".join(p for p in parts_c if p))

    prompt = " | ".join(sections)

    # Warn when no visual event
    if not event_raw or event_raw == "（未設定）":
        prompt = _NO_EVENT_WARNING + " " + prompt

    return prompt


def assemble_video_prompt(scene, characters, scene_groups, production, model_mode: str = "通用") -> str:
    """Build a video / animation prompt — 4-part structure."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    sentences: list[str] = []

    # ── B. Asset lock ─────────────────────────────────────────────────────────
    lock_text = _build_asset_lock_text(scene, characters)
    if lock_text:
        sentences.append(lock_text)

    rr = getattr(production, "relationship_rules", "").strip()
    if rr:
        sentences.append(rr)

    # ── C. Scene event ────────────────────────────────────────────────────────
    event_raw = getattr(scene, "event", "").strip()
    if event_raw and event_raw != "（未設定）":
        evt_en = EVENT_TYPES_EN.get(event_raw, event_raw)
        if evt_en:
            sentences.append(f"Scene event: {evt_en}.")

    interaction = getattr(scene, "interaction", "").strip()
    if interaction:
        sentences.append(f"Interaction: {interaction}.")

    # 3-phase animation
    start_state = getattr(scene, "start_state", "").strip()
    main_action = getattr(scene, "main_action", "").strip()
    end_state   = getattr(scene, "end_state", "").strip()

    if mode.get("segmented", True) and (start_state or main_action or end_state):
        if start_state:
            sentences.append(f"At the beginning, {start_state}.")
        if main_action:
            sentences.append(main_action if main_action.endswith(".") else main_action + ".")
        if end_state:
            sentences.append(f"Finally, {end_state}.")
    else:
        action_desc = _build_char_action_desc(scene, characters)
        if action_desc:
            sentences.append(action_desc)

    # Camera movement
    movement = getattr(scene, "camera_movement", "固定")
    if movement and movement != "固定":
        move_en = CAMERA_MOVEMENTS_EN.get(movement, movement)
        speed   = getattr(scene, "camera_speed", "緩慢")
        speed_en = CAMERA_SPEEDS_EN.get(speed, "")
        stab  = getattr(scene, "camera_stability", "穩定")
        stab_en = CAMERA_STABILITY_EN.get(stab, "")
        cam_parts = ["The camera"]
        if speed_en:
            cam_parts.append(speed_en)
        cam_parts.append(move_en)
        if stab_en and stab != "穩定":
            cam_parts.append(f"({stab_en})")
        sentences.append(" ".join(cam_parts) + ".")

    # Environment dynamics
    env_dynamics = getattr(scene, "env_dynamics", [])
    if env_dynamics:
        dyn_en = [ENV_DYNAMICS_EN[d] for d in env_dynamics if d in ENV_DYNAMICS_EN]
        if dyn_en:
            sentences.append(", ".join(dyn_en) + ".")

    # Emotions → atmosphere
    emotions = getattr(scene, "emotions", [])
    if emotions:
        emo_en = [EMOTIONS_EN[e] for e in emotions if e in EMOTIONS_EN]
        if emo_en:
            sentences.append(f"Atmosphere: {', '.join(emo_en)}.")

    # Text safe area
    safe_en = TEXT_SAFE_AREA_EN.get(getattr(scene, "text_safe_area", "無"), "")
    if safe_en:
        sentences.append(safe_en + ".")

    if mode.get("video_tags", False):
        sentences.append("Natural body motion, smooth animation, stable character identity.")

    prompt = " ".join(s for s in sentences if s)

    if not event_raw or event_raw == "（未設定）":
        prompt = _NO_EVENT_WARNING + " " + prompt

    return prompt


def assemble_negative_prompt(scene, model_mode: str = "通用") -> str:
    """Build the negative prompt — universal constraints + user-selected options."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    if not mode.get("negative", True):
        return ""
    parts = [_UNIVERSAL_NEGATIVE]
    negative_opts = getattr(scene, "negative_opts", [])
    user_parts = [NEGATIVE_PROMPTS_EN[o] for o in negative_opts if o in NEGATIVE_PROMPTS_EN]
    if user_parts:
        parts.append(", ".join(user_parts))
    return " ".join(p for p in parts if p)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_asset_lock_text(scene, characters) -> str:
    """Build the B-section asset consistency lock sentence for scene assets."""
    char_indices = getattr(scene, "char_indices", [])
    if not char_indices or not characters:
        return ""
    has_ref = any(
        getattr(characters[ci], "reference_image_path", "").strip()
        for ci in char_indices if 0 <= ci < len(characters)
    )
    ref_clause = (
        "from the approved reference image(s)" if has_ref
        else "WARNING: no reference image set — identity may drift"
    )
    names: list[str] = []
    for ci in char_indices:
        if 0 <= ci < len(characters):
            c = characters[ci]
            n = c.name if c.name != "角色" else f"character {ci + 1}"
            names.append(n)
    subjects = ", ".join(names) if names else "the characters in this scene"
    return (
        f"Asset consistency lock for {subjects}: "
        f"Keep the exact identity, face/body or product shape, scale relationships, "
        f"hairstyle/fur/material, default appearance state, wardrobe, accessories and "
        f"defining details {ref_clause}. "
        f"Do not add, remove, duplicate, merge or swap assets. "
        f"Keep natural eye line and pose unless this scene explicitly overrides it."
    )

def _build_environment(scene, scene_groups, production) -> str:
    """Scene group environment OR per-scene fields OR global production settings."""
    # Try scene group first
    sg_id = getattr(scene, "scene_group_id", "")
    if sg_id and scene_groups:
        for sg in scene_groups:
            if getattr(sg, "id", "") == sg_id:
                ep = getattr(sg, "environment_prompt", "")
                if ep:
                    return ep
                break

    # Per-scene overrides
    parts: list[str] = []
    loc = getattr(scene, "scene_location", "")
    if loc and loc != "（無）":
        loc_en = SCENE_LOCATIONS_EN.get(loc, loc)
        if loc_en:
            parts.append(loc_en)

    t = getattr(scene, "scene_time", "不指定")
    if t and t != "不指定":
        parts.append(TIMES_EN.get(t, ""))

    w = getattr(scene, "weather", "不指定")
    if w and w != "不指定":
        parts.append(WEATHER_EN.get(w, ""))

    # Global production fallback
    era_map = {
        "遠古": "ancient prehistoric era", "古代": "ancient historical era",
        "近代": "early modern era",         "現代": "modern contemporary",
        "未來": "futuristic",               "架空": "fictional fantasy world",
    }
    if not parts:
        if hasattr(production, "era") and production.era and production.era != "現代":
            parts.append(era_map.get(production.era, production.era))
        if hasattr(production, "location") and production.location:
            parts.append(production.location)
    if hasattr(production, "bg_desc") and production.bg_desc:
        parts.append(production.bg_desc)

    return ", ".join(p for p in parts if p)


def _build_char_image_desc(scene, characters) -> str:
    """Full character Bible description for each character in the scene."""
    char_indices = getattr(scene, "char_indices", [])
    if not char_indices or not characters:
        return ""
    descs: list[str] = []
    for ci in char_indices:
        if ci < 0 or ci >= len(characters):
            continue
        c = characters[ci]
        prompt = _char_full_prompt(c)
        if prompt:
            descs.append(prompt)
    return "; ".join(descs)


def _char_full_prompt(char) -> str:
    """Convert a Character to its English image prompt string."""
    parts: list[str] = []
    age_map = {
        "幼兒": "toddler", "少年": "teenager", "青年": "young adult",
        "中年": "middle-aged", "老年": "elderly",
    }
    gender_map = {"男": "male", "女": "female", "不限": ""}
    g = gender_map.get(getattr(char, "gender", "男"), "")
    a = age_map.get(getattr(char, "age", "青年"), "")
    if g:
        parts.append(g)
    if a:
        parts.append(a)
    for attr in ("body_type", "hair", "face", "clothing_top", "clothing_bottom",
                 "clothing_shoes", "accessories", "appearance"):
        val = getattr(char, attr, "").strip()
        if val:
            parts.append(val)
    return ", ".join(p for p in parts if p)


def _build_consistency_clause(scene, characters) -> str:
    """Append consistency terms for characters with consistency_lock=True."""
    char_indices = getattr(scene, "char_indices", [])
    terms: list[str] = []
    for ci in char_indices:
        if ci < 0 or ci >= len(characters):
            continue
        c = characters[ci]
        if getattr(c, "consistency_lock", True):
            ct = getattr(c, "consistency_terms", "").strip()
            terms.append(ct if ct else "same character design, consistent facial features")
    if not terms:
        return ""
    unique = list(dict.fromkeys(terms))
    return "; ".join(unique) + "."


def _build_char_action_desc(scene, characters) -> str:
    """Fallback action description from char_actions dict."""
    char_actions = getattr(scene, "char_actions", {})
    char_expressions = getattr(scene, "char_expressions", {})
    char_indices = getattr(scene, "char_indices", [])
    if not char_actions or not char_indices:
        return ""
    parts: list[str] = []
    for ci in char_indices:
        if ci < 0 or ci >= len(characters):
            continue
        key = str(ci)
        action = char_actions.get(key, "（無）")
        expr   = char_expressions.get(key, "（無）")
        if action and action != "（無）":
            c = characters[ci]
            name = getattr(c, "name", f"character {ci+1}")
            if name == "角色":
                name = f"character {ci+1}"
            desc = f"{name} {action}"
            expr_en = EXPRESSIONS_EN.get(expr, "")
            if expr_en:
                desc += f", {expr_en}"
            parts.append(desc)
    return "; ".join(parts)
