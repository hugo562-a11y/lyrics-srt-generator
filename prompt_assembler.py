"""Prompt assembler — produces image_prompt, video_prompt, negative_prompt per scene."""
from __future__ import annotations

from storyboard_data import (
    SHOT_TYPE_EN, COLOR_TONE_EN,
    CAMERA_ANGLES_EN, CAMERA_MOVEMENTS_EN, CAMERA_SPEEDS_EN, CAMERA_STABILITY_EN,
    COMPOSITIONS_EN, ORIENTATIONS_EN,
    ENV_DYNAMICS_EN, EMOTIONS_EN,
    NEGATIVE_PROMPTS_EN, EXPRESSIONS_EN, GAZE_EN,
    TIMES_EN, WEATHER_EN, SCENE_LOCATIONS_EN,
    MODEL_MODES,
)
from prompts import PROMPT_STYLE_MAP


def assemble_image_prompt(scene, characters, scene_groups, production, model_mode: str = "通用") -> str:
    """Build a static image / keyframe prompt from the scene data."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    parts: list[str] = []

    # Style
    style_desc = PROMPT_STYLE_MAP.get(getattr(scene, "style", "電影風"), "high quality digital art")
    parts.append(style_desc)

    # Shot type
    shot_en = SHOT_TYPE_EN.get(getattr(scene, "shot_type", "中景"), "")
    if shot_en:
        parts.append(shot_en)

    # Color tone
    tone_en = COLOR_TONE_EN.get(getattr(scene, "tone", "暖色"), "")
    if tone_en:
        parts.append(tone_en)

    # Environment: scene group → per-scene → global production
    env = _build_environment(scene, scene_groups, production)
    if env:
        parts.append(env)

    # Characters with full Bible description
    char_desc = _build_char_image_desc(scene, characters)
    if char_desc:
        parts.append(char_desc)

    # Composition — generic keys look up EN; dynamic char-position values used verbatim
    comp_raw = getattr(scene, "composition", "（無）")
    comp_en = COMPOSITIONS_EN.get(comp_raw, comp_raw if comp_raw not in ("（無）", "", "── 角色位置 ──") else "")
    if comp_en:
        parts.append(comp_en)

    # Orientation
    ori_en = ORIENTATIONS_EN.get(getattr(scene, "orientation", "（無）"), "")
    if ori_en:
        parts.append(ori_en)

    # Camera angle (static image: angle is relevant, movement is not)
    angle_en = CAMERA_ANGLES_EN.get(getattr(scene, "camera_angle", "平視"), "")
    if angle_en and angle_en != CAMERA_ANGLES_EN.get("平視"):
        parts.append(angle_en)

    # Emotions → lighting / atmosphere hint
    emotions = getattr(scene, "emotions", [])
    if emotions:
        emo_en = [EMOTIONS_EN[e] for e in emotions if e in EMOTIONS_EN]
        if emo_en:
            parts.append(", ".join(emo_en))

    # Quality tags
    if mode.get("image_tags", True):
        parts.append("masterpiece, best quality, highly detailed")

    return ", ".join(p for p in parts if p)


def assemble_video_prompt(scene, characters, scene_groups, production, model_mode: str = "通用") -> str:
    """Build a video / animation prompt from the scene data."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    sentences: list[str] = []

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
        # Fallback: per-character actions from char_actions dict
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

    # Character consistency terms
    consistency = _build_consistency_clause(scene, characters)
    if consistency:
        sentences.append(consistency)

    if mode.get("video_tags", False):
        sentences.append("Natural body motion, smooth animation, stable character identity.")

    return " ".join(s for s in sentences if s)


def assemble_negative_prompt(scene, model_mode: str = "通用") -> str:
    """Build the negative prompt if the model supports it."""
    mode = MODEL_MODES.get(model_mode, MODEL_MODES["通用"])
    if not mode.get("negative", True):
        return ""
    negative_opts = getattr(scene, "negative_opts", [])
    parts = [NEGATIVE_PROMPTS_EN[o] for o in negative_opts if o in NEGATIVE_PROMPTS_EN]
    return ", ".join(parts)


# ── Helpers ───────────────────────────────────────────────────────────────────

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
