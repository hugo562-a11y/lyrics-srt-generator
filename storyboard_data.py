"""Storyboard data definitions — lookup tables, constants, rules."""
from __future__ import annotations

SCHEMA_VERSION = 2

# ── Shot & tone (mirrors app.py; canonical source for prompt_assembler) ──────

SHOT_TYPES = ["大遠景", "遠景", "全景", "中景", "中近景", "近景", "特寫", "俯拍", "仰拍", "主觀"]
COLOR_TONES = ["暖色", "冷色", "黑白", "復古", "高反差", "低飽和", "夕陽橙", "夜藍", "霓虹"]

SHOT_TYPE_EN: dict[str, str] = {
    "大遠景": "extreme long shot", "遠景": "long shot", "全景": "full shot",
    "中景": "medium shot", "中近景": "medium close-up", "近景": "close-up",
    "特寫": "extreme close-up", "俯拍": "bird's eye view",
    "仰拍": "low angle shot", "主觀": "POV shot",
}
COLOR_TONE_EN: dict[str, str] = {
    "暖色": "warm color tones, golden light",
    "冷色": "cool blue tones, cold atmosphere",
    "黑白": "black and white, monochrome",
    "復古": "vintage film look, retro colors, aged",
    "高反差": "high contrast, dramatic shadows",
    "低飽和": "desaturated, muted colors",
    "夕陽橙": "golden hour, sunset orange glow",
    "夜藍": "night blue, midnight atmosphere",
    "霓虹": "neon lights, vibrant glowing colors",
}

# ── Camera ────────────────────────────────────────────────────────────────────

CAMERA_ANGLES = [
    "平視", "低角度", "高角度", "俯視", "仰視", "過肩", "手部特寫",
]
CAMERA_MOVEMENTS = [
    "固定", "慢速推近", "慢速拉遠", "左平移", "右平移", "平穩跟拍",
    "環繞", "升起", "下降", "航拍下降", "輕微手持", "由手移至臉", "拉遠揭示夕陽",
]
CAMERA_SPEEDS = ["極慢", "緩慢", "一般", "快速"]
CAMERA_STABILITY = ["穩定", "輕微手持", "強烈手持"]

CAMERA_ANGLES_EN: dict[str, str] = {
    "平視": "eye-level angle",
    "低角度": "low angle shot",
    "高角度": "high angle shot",
    "俯視": "bird's eye view",
    "仰視": "extreme low angle",
    "過肩": "over-the-shoulder shot",
    "手部特寫": "close-up on hands",
}
CAMERA_MOVEMENTS_EN: dict[str, str] = {
    "固定": "static shot",
    "慢速推近": "slow dolly in",
    "慢速拉遠": "slow dolly out",
    "左平移": "slow pan left",
    "右平移": "slow pan right",
    "平穩跟拍": "smooth tracking shot",
    "環繞": "slow orbit around subjects",
    "升起": "crane up",
    "下降": "crane down",
    "航拍下降": "aerial descending shot",
    "輕微手持": "slight handheld movement",
    "由手移至臉": "camera tilts from hands up to faces",
    "拉遠揭示夕陽": "pull back to reveal sunset",
}
CAMERA_SPEEDS_EN: dict[str, str] = {
    "極慢": "very slowly",
    "緩慢": "slowly",
    "一般": "",
    "快速": "quickly",
}
CAMERA_STABILITY_EN: dict[str, str] = {
    "穩定": "stable footage",
    "輕微手持": "slight handheld movement",
    "強烈手持": "strong handheld movement",
}

# ── Composition & orientation ─────────────────────────────────────────────────

COMPOSITIONS = [
    "（無）",
    "三分法", "中央構圖", "對稱構圖", "三角構圖",
    "引導線", "前景框景", "黃金比例",
    "主體偏左", "主體偏右", "主體偏上", "主體偏下",
    "並排", "一前一後", "群體縱深",
]
ORIENTATIONS = [
    "（無）", "正面", "側面", "背面", "三分之二側面",
    "面向彼此", "面向夕陽", "面向遠方",
]
COMPOSITIONS_EN: dict[str, str] = {
    "（無）": "",
    "三分法": "rule of thirds",
    "中央構圖": "centered composition",
    "對稱構圖": "symmetrical composition",
    "三角構圖": "triangular composition",
    "引導線": "leading lines composition",
    "前景框景": "foreground framing",
    "黃金比例": "golden ratio composition",
    "主體偏左": "subject positioned on the left",
    "主體偏右": "subject positioned on the right",
    "主體偏上": "subject positioned in the upper frame",
    "主體偏下": "subject positioned in the lower frame",
    "並排": "subjects side by side",
    "一前一後": "one subject in foreground, one behind",
    "群體縱深": "group with layered depth",
}
ORIENTATIONS_EN: dict[str, str] = {
    "（無）": "",
    "正面": "facing the camera",
    "側面": "profile view",
    "背面": "seen from behind",
    "三分之二側面": "three-quarter view",
    "面向彼此": "facing each other",
    "面向夕陽": "facing the sunset",
    "面向遠方": "gazing into the distance",
}

# ── Character Bible presets ───────────────────────────────────────────────────

BODY_TYPES = [
    "（無）", "纖細", "苗條", "普通", "高挑", "矮小",
    "壯碩", "健美", "略胖", "豐腴",
]
HAIR_STYLES = [
    "（無）",
    "黑色短髮", "黑色中長髮", "黑色長直髮", "黑色馬尾", "黑色捲髮", "黑色雙馬尾",
    "棕色短髮", "棕色長直髮", "棕色捲髮",
    "金色短髮", "金色長髮", "金色捲髮",
    "白色短髮", "白色長髮",
    "深色馬尾", "深色雙馬尾", "深色辮子",
    "光頭",
]
FACE_FEATURES = [
    "（無）", "清秀俊朗", "帥氣", "可愛", "甜美", "成熟穩重",
    "圓臉", "瓜子臉", "方臉", "娃娃臉",
    "深邃眼神", "溫柔眼神", "眼神堅定", "眉目清秀",
]
TOP_STYLES = [
    "（無）",
    "白T恤", "黑T恤", "灰T恤",
    "白色襯衫", "格紋襯衫", "紅色格紋襯衫",
    "連帽外套", "牛仔外套", "夾克",
    "西裝外套", "毛衣", "無袖背心", "風衣",
    "紅色披風",
]
BOTTOM_STYLES = [
    "（無）",
    "牛仔褲", "黑色牛仔褲", "淺色牛仔褲",
    "休閒長褲", "西裝褲", "運動褲",
    "短褲", "裙子", "長裙", "百褶裙",
]
SHOE_STYLES = [
    "（無）",
    "白色運動鞋", "黑色運動鞋", "彩色運動鞋",
    "皮鞋", "黑色皮鞋",
    "涼鞋", "拖鞋",
    "靴子", "短靴", "休閒鞋",
]
ACCESSORY_OPTIONS = [
    "（無）",
    "眼鏡", "太陽眼鏡",
    "手錶", "腕表",
    "項鍊", "戒指", "手環", "耳環",
    "棒球帽", "毛帽", "頭帶",
    "背包",
    "耳機", "圍巾",
]

# ── Animation phase presets ───────────────────────────────────────────────────

ANIMATION_STATES = [
    "（無）",
    "靜靜站立", "坐著不動", "蹲下", "跪著",
    "背對鏡頭", "側身站立", "低頭沉思", "抬頭望向遠方",
    "閉上眼睛", "張開雙臂", "伸出雙手", "靜靜擁抱",
    "回頭望向鏡頭", "凝視遠方", "微笑站立",
    "趴在地上", "躺著", "倚靠著牆",
]
ANIMATION_ACTIONS = [
    "（無）",
    "緩緩走向鏡頭", "緩緩走離鏡頭", "緩緩靠近對方",
    "奔跑過去", "跑向彼此", "緩緩離去",
    "轉身回頭", "回頭微笑", "跳起來",
    "伸出手牽住", "緊緊擁抱", "輕拍肩膀",
    "蹲下陪玩", "抱起孩子", "低頭看向孩子",
    "揮手", "指向遠方", "遞出物品",
    "披上披風", "保護孩子", "陪伴同行",
    "坐下來", "站起身", "轉身面向鏡頭",
    "緩緩抬起頭", "緩緩低下頭",
]

# ── Actions, expressions, gaze ────────────────────────────────────────────────

ACTIONS_GENERAL = [
    "（無）", "站立", "走路", "奔跑", "擁抱", "揮手", "牽手", "蹲下",
    "跳躍", "坐著", "回頭", "轉身", "指向遠方", "低頭", "張開雙臂",
    "抱起孩子", "保護孩子", "低頭看孩子", "陪玩", "披上披風",
    "跑向爸爸", "遞出布丁", "指向甲蟲", "坐在身旁", "躲在身後",
]
EXPRESSIONS = [
    "（無）", "溫柔微笑", "開懷大笑", "驚訝", "感動", "安心", "好奇",
    "興奮", "調皮", "害怕", "自豪", "眼眶泛淚", "嚴肅", "開心",
]
GAZE_OPTIONS = [
    "（無）", "看鏡頭", "看爸爸", "看姐姐", "看妹妹", "看遠方",
    "看手中物品", "閉眼擁抱", "彼此注視", "低頭", "看天空",
]

EXPRESSIONS_EN: dict[str, str] = {
    "（無）": "", "溫柔微笑": "gentle smile", "開懷大笑": "laughing heartily",
    "驚訝": "surprised", "感動": "moved and touched", "安心": "relieved and at ease",
    "好奇": "curious", "興奮": "excited", "調皮": "playful and mischievous",
    "害怕": "slightly scared", "自豪": "proud", "眼眶泛淚": "eyes glistening with tears",
    "嚴肅": "serious", "開心": "joyful",
}
GAZE_EN: dict[str, str] = {
    "（無）": "", "看鏡頭": "looking at the camera", "看爸爸": "looking at father",
    "看姐姐": "looking at older sister", "看妹妹": "looking at younger sister",
    "看遠方": "gazing into the distance", "看手中物品": "looking at the object in hand",
    "閉眼擁抱": "eyes closed in embrace", "彼此注視": "gazing at each other",
    "低頭": "looking down", "看天空": "looking up at the sky",
}

# ── Environment dynamics ──────────────────────────────────────────────────────

ENV_DYNAMICS = [
    "頭髮隨微風飄動", "衣服隨微風飄動", "披風隨風飄動", "裙擺隨風飄動",
    "草地搖曳", "樹葉搖曳", "陽光閃爍", "水面閃爍", "光斑漂浮",
    "塵埃漂浮", "車輛緩慢移動", "背景人物走動",
]
ENV_DYNAMICS_EN: dict[str, str] = {
    "頭髮隨微風飄動": "hair gently flowing in the breeze",
    "衣服隨微風飄動": "clothes softly swaying in the wind",
    "披風隨風飄動": "cape flowing dramatically in the wind",
    "裙擺隨風飄動": "dress hem swaying in the breeze",
    "草地搖曳": "grass swaying gently",
    "樹葉搖曳": "leaves rustling in the breeze",
    "陽光閃爍": "sunlight glimmering through the leaves",
    "水面閃爍": "water surface shimmering",
    "光斑漂浮": "light particles floating in the air",
    "塵埃漂浮": "dust motes drifting in golden light",
    "車輛緩慢移動": "vehicles slowly moving in the background",
    "背景人物走動": "background people walking naturally",
}

# ── Emotions ──────────────────────────────────────────────────────────────────

EMOTIONS = [
    "溫馨", "幽默", "英雄感", "安全感", "興奮", "感動",
    "幸福", "童真", "懷念", "真摯",
]
EMOTIONS_EN: dict[str, str] = {
    "溫馨": "warm and heartfelt",
    "幽默": "lighthearted humor",
    "英雄感": "heroic and inspiring",
    "安全感": "safe and protected",
    "興奮": "exciting and energetic",
    "感動": "deeply touching",
    "幸福": "blissful happiness",
    "童真": "childlike innocence",
    "懷念": "nostalgic",
    "真摯": "sincere and genuine",
}

# ── Negative options ──────────────────────────────────────────────────────────

NEGATIVE_OPTIONS = [
    "不增加人物", "不改服裝", "不改髮型", "不變形", "不多手多腳",
    "不穿模", "不瞬間移動", "不突然轉身", "不看鏡頭", "不抖動",
    "不快速縮放", "不閃爍",
]
NEGATIVE_PROMPTS_EN: dict[str, str] = {
    "不增加人物": "no extra people, no additional characters",
    "不改服裝": "no clothing change, consistent outfit",
    "不改髮型": "no hairstyle change, consistent hair",
    "不變形": "no deformation, no morphing",
    "不多手多腳": "no extra hands, no duplicated limbs, no deformed fingers",
    "不穿模": "no clipping, no character penetration",
    "不瞬間移動": "no teleportation, no sudden position change",
    "不突然轉身": "no abrupt turn, smooth rotation only",
    "不看鏡頭": "no breaking the fourth wall",
    "不抖動": "no flickering, no jitter",
    "不快速縮放": "no sudden zoom, no abrupt scale change",
    "不閃爍": "no light flickering, stable exposure",
}

# ── Scene environment ─────────────────────────────────────────────────────────

SCENE_LOCATIONS = [
    "（無）", "客廳", "廚房", "車內", "河濱公園", "草地", "森林", "遊樂場",
    "城市街道", "學校", "海灘", "山頂", "屋頂", "廣場", "公園小徑",
    "家門口", "後院",
]
TIMES_OF_DAY = ["不指定", "清晨", "上午", "中午", "下午", "黃昏", "夜晚"]
WEATHER_OPTIONS = ["不指定", "晴朗", "多雲", "微風", "雨天", "薄霧", "雪天"]

SCENE_LOCATIONS_EN: dict[str, str] = {
    "（無）": "", "客廳": "living room", "廚房": "kitchen", "車內": "inside a car",
    "河濱公園": "riverside park", "草地": "open grassy field", "森林": "forest",
    "遊樂場": "playground", "城市街道": "city street", "學校": "school campus",
    "海灘": "sandy beach", "山頂": "mountaintop", "屋頂": "rooftop",
    "廣場": "town square", "公園小徑": "park path", "家門口": "front of the house",
    "後院": "backyard",
}
TIMES_EN: dict[str, str] = {
    "不指定": "", "清晨": "early morning, soft dawn light",
    "上午": "morning, clear daylight", "中午": "midday, bright sunlight",
    "下午": "afternoon, warm afternoon light", "黃昏": "golden hour, sunset glow",
    "夜晚": "nighttime, warm artificial lighting",
}
WEATHER_EN: dict[str, str] = {
    "不指定": "", "晴朗": "clear blue sky", "多雲": "partly cloudy sky",
    "微風": "gentle breeze", "雨天": "light rain, glistening wet surfaces",
    "薄霧": "light morning mist", "雪天": "soft snowfall",
}

# ── Model output modes ────────────────────────────────────────────────────────

MODEL_MODES: dict[str, dict] = {
    "通用":    {"negative": True, "segmented": True, "first_last": False, "video_tags": False, "image_tags": True},
    "Kling":   {"negative": True, "segmented": True, "first_last": True,  "video_tags": False, "image_tags": True},
    "Runway":  {"negative": True, "segmented": False,"first_last": False, "video_tags": True,  "image_tags": False},
    "Veo":     {"negative": False,"segmented": True, "first_last": False, "video_tags": True,  "image_tags": False},
    "Hailuo":  {"negative": True, "segmented": True, "first_last": True,  "video_tags": False, "image_tags": True},
    "PixVerse":{"negative": True, "segmented": False,"first_last": False, "video_tags": True,  "image_tags": True},
    "即夢":    {"negative": True, "segmented": True, "first_last": True,  "video_tags": False, "image_tags": True},
}
MODEL_MODE_NAMES = list(MODEL_MODES.keys())

# ── Lyric keyword rules ───────────────────────────────────────────────────────

LYRIC_RULES = [
    {"keywords": ["爸爸", "父親"], "suggestions": {"shot_type": "近景", "emotion": "溫馨"}},
    {"keywords": ["超人", "英雄"], "suggestions": {"camera_angle": "低角度", "camera_movement": "慢速推近", "env_dynamic": "披風隨風飄動", "emotion": "英雄感"}},
    {"keywords": ["肚子", "胖"],  "suggestions": {"shot_type": "全景", "emotion": "幽默"}},
    {"keywords": ["開車", "大車車", "車"], "suggestions": {"scene_location": "車內", "camera_movement": "平穩跟拍"}},
    {"keywords": ["甲蟲", "蟲"],  "suggestions": {"scene_location": "草地", "camera_angle": "低角度", "emotion": "好奇"}},
    {"keywords": ["不怕", "保護", "安全"], "suggestions": {"camera_angle": "低角度", "emotion": "安全感"}},
    {"keywords": ["布丁"],        "suggestions": {"shot_type": "中近景", "camera_movement": "慢速推近", "emotion": "幸福"}},
    {"keywords": ["牽手", "拉手"],"suggestions": {"camera_angle": "手部特寫", "camera_movement": "由手移至臉", "emotion": "溫馨"}},
    {"keywords": ["笑", "開心", "快樂"], "suggestions": {"camera_movement": "平穩跟拍", "env_dynamic": "頭髮隨微風飄動", "emotion": "幸福"}},
]

# ── Scene templates ───────────────────────────────────────────────────────────

SCENE_TEMPLATES: dict[str, dict] = {
    "英雄爸爸": {
        "description": "黃昏戶外，紅披風，低角度慢推，英雄感",
        "fields": {
            "scene_time": "黃昏", "scene_location": "河濱公園", "weather": "晴朗",
            "camera_angle": "低角度", "camera_movement": "慢速推近",
            "camera_speed": "緩慢", "camera_stability": "穩定",
            "env_dynamics": ["披風隨風飄動"], "emotions": ["英雄感", "溫馨"],
            "composition": "三角構圖",
        },
    },
    "家庭互動": {
        "description": "河濱公園，平穩跟拍，自然幸福",
        "fields": {
            "scene_location": "河濱公園", "scene_time": "下午",
            "camera_movement": "平穩跟拍", "camera_speed": "緩慢",
            "camera_stability": "穩定", "emotions": ["溫馨", "幸福"],
            "composition": "並排",
        },
    },
    "情感擁抱": {
        "description": "夕陽，孩子跑向爸爸，環繞鏡頭，感動",
        "fields": {
            "scene_time": "黃昏", "camera_movement": "環繞",
            "camera_speed": "緩慢", "camera_stability": "穩定",
            "emotions": ["感動", "溫馨"],
            "env_dynamics": ["頭髮隨微風飄動", "衣服隨微風飄動"],
        },
    },
    "物品互動": {
        "description": "物品遞送，中近景，慢速推近，幸福",
        "fields": {
            "shot_type": "中近景", "camera_movement": "慢速推近",
            "camera_speed": "緩慢", "emotions": ["幸福", "溫馨"],
        },
    },
}
