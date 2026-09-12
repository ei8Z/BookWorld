"""数据库 Schema——创建所有表结构。"""


SCHEMA_SQL = """
-- 玩家信息（单行表）
CREATE TABLE IF NOT EXISTS player (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT '林越',
    age INTEGER DEFAULT 24,
    height_cm INTEGER DEFAULT 175,
    weight_kg REAL DEFAULT 62.0,
    title TEXT DEFAULT '',
    currency INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1,
    current_exp INTEGER DEFAULT 0,
    max_exp INTEGER DEFAULT 100,
    hp INTEGER DEFAULT 100,
    max_hp INTEGER DEFAULT 100,
    atk INTEGER DEFAULT 10,
    en INTEGER DEFAULT 100,
    max_en INTEGER DEFAULT 100,
    status TEXT DEFAULT '正常',
    core_trait TEXT DEFAULT '变形者',
    appearance_bonus INTEGER DEFAULT 0
);

-- 玩家装备槽
CREATE TABLE IF NOT EXISTS player_equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '未穿戴',
    name TEXT NOT NULL DEFAULT '空',
    integrity INTEGER NOT NULL DEFAULT 100
);

-- 故事状态（单行表）
CREATE TABLE IF NOT EXISTS story_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_date TEXT NOT NULL DEFAULT '2026年06月22日 星期一',
    current_time TEXT NOT NULL DEFAULT '上午 08:00',
    current_scene TEXT DEFAULT '',
    chapter INTEGER DEFAULT 1
);

-- 临时记忆
CREATE TABLE IF NOT EXISTS temp_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    datetime_str TEXT NOT NULL,
    scene TEXT NOT NULL,
    event TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 永久记忆
CREATE TABLE IF NOT EXISTS perm_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    datetime_range TEXT NOT NULL,
    scene TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 剧情摘要（单行，始终保留最新）
CREATE TABLE IF NOT EXISTS plot_summary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL DEFAULT ''
);

-- 重要情报
CREATE TABLE IF NOT EXISTS important_intel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- NPC
CREATE TABLE IF NOT EXISTS npcs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    gender TEXT NOT NULL,
    occupation TEXT DEFAULT '',
    appearance TEXT DEFAULT '',
    personality TEXT DEFAULT '',
    equipment TEXT DEFAULT '无',
    affection_level INTEGER DEFAULT 1,
    affection_exp INTEGER DEFAULT 0,
    affection_exp_max INTEGER DEFAULT 100,
    sex_count INTEGER DEFAULT 0,
    inner_thought TEXT DEFAULT '',
    intel TEXT DEFAULT '',
    status TEXT DEFAULT '正常',
    is_present INTEGER DEFAULT 0,
    h_preferences TEXT DEFAULT ''
);

-- 关系网络
CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_a TEXT NOT NULL,
    character_b TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    description TEXT DEFAULT ''
);

-- 重要道具
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    quality TEXT DEFAULT '普通',
    status TEXT DEFAULT '完好'
);

-- 区域信息
CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    security TEXT DEFAULT '普通区',
    danger TEXT DEFAULT '无威胁',
    entry_requirement TEXT DEFAULT '无',
    landmarks TEXT DEFAULT ''
);

-- 建议行动
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    conservative TEXT DEFAULT '',
    aggressive TEXT DEFAULT '',
    creative TEXT DEFAULT ''
);

-- 会话历史（保存每次生成的正文和元数据）
CREATE TABLE IF NOT EXISTS session_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number INTEGER NOT NULL,
    user_input TEXT,
    narrative TEXT NOT NULL,
    story_state_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 故事设定（注入 Writer 系统提示词的核心配置）
CREATE TABLE IF NOT EXISTS story_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_temp_memories_seq ON temp_memories(seq);
CREATE INDEX IF NOT EXISTS idx_perm_memories_seq ON perm_memories(seq);
CREATE INDEX IF NOT EXISTS idx_npcs_name ON npcs(name);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
"""

INIT_SQL = """
-- 初始化默认数据
INSERT OR IGNORE INTO player (id) VALUES (1);
INSERT OR IGNORE INTO story_state (id) VALUES (1);
INSERT OR IGNORE INTO plot_summary (id) VALUES (1);
INSERT OR IGNORE INTO suggestions (id) VALUES (1);

-- 默认装备槽
INSERT OR IGNORE INTO player_equipment (id, slot, category, name, integrity) VALUES (1, '头部', '未穿戴', '空', 100);
INSERT OR IGNORE INTO player_equipment (id, slot, category, name, integrity) VALUES (2, '躯干', '未穿戴', '空', 100);
INSERT OR IGNORE INTO player_equipment (id, slot, category, name, integrity) VALUES (3, '左手', '未穿戴', '空', 100);
INSERT OR IGNORE INTO player_equipment (id, slot, category, name, integrity) VALUES (4, '右手', '未穿戴', '空', 100);
INSERT OR IGNORE INTO player_equipment (id, slot, category, name, integrity) VALUES (5, '腿部', '未穿戴', '空', 100);

-- 默认故事设定
INSERT OR IGNORE INTO story_settings (key, value) VALUES ('player_profile', '姓名：林越。年龄：二十四岁。身高/体重：175cm / 62kg。体态特征：体脂低，骨架偏窄，锁骨清晰，肩宽适中，腰细，小臂小腿有匀称薄肌，背面观肩窄腰细，有少年感。隐秘特质：有变装与恋物倾向，迷恋女性贴身衣物及露出的刺激感。');
INSERT OR IGNORE INTO story_settings (key, value) VALUES ('player_abilities', '能力身份：变形者。能力细则：【完全外形复制】可精确复制目标的面部、虹膜、指纹、肤纹、身高、体型、声音、体味，及衣物和随身小物件（眼镜、手套、首饰），维持变形无需消耗精力，无时间限制。【基础身体改造】可微调手指长度、局部皮肤增厚、生长小型角质爪或鳞片，不能变出真正的武器或工具。【被动生存强化】非致命伤数小时至数天自愈，断肢需数周至数月，寿命极长，对多数毒素、疾病、药物有强抵抗力。【生殖系统完全转变】可变出完整功能性生殖系统（卵巢/子宫/阴道或睾丸/输精管），女性形态下可排卵，男性形态下可产生精子，可正常受孕或使他人受孕。【模板记录机制】女性模板首次构建需1天专注，完成后永久记忆；男性模板首次构建需1天专注，完成后永久记忆。两套模板就绪后，所有性别切换及外观调整均为几秒内完成，精力消耗极低。不涉及生殖系统的外观调整始终为几秒完成。【切换副作用】两套模板就绪后的性别切换后会经历15-30分钟生理调整期（神经校准、激素波动、身体虚弱、触觉敏感、易兴奋），仅变外观无此副作用。首次构建模板时副作用更剧烈。【质量守恒】体重不变，变大个体会松垮，变个体会紧实，可重新分配质量优化负重。【能力边界】不复制超能力，必须保持人形，重伤/昏迷/睡眠时变形解除或异常，热成像/骨密度扫描/基因检测可能识破。');
INSERT OR IGNORE INTO story_settings (key, value) VALUES ('world_setting', '时代/世界类型：当前现代社会。核心规则：遵循当前现代社会人际关系和社交规则，无魔法、无系统、无超自然元素（除主角变形能力外）。故事基调：写实向都市题材，注重人物心理和社会关系。');
INSERT OR IGNORE INTO story_settings (key, value) VALUES ('initial_setup', ' 故事起点：城市高新区。林越作为一名拥有变形能力的普通人，在都市中隐藏自己的秘密，同时利用能力满足隐秘欲望。开场氛围：都市日常，略带情色张力。');
"""
