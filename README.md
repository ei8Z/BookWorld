# Book 叙事引擎

将原始单体 AI 提示词拆分为 **作家 (Writer)**、**系统执行者 (Executor)**、**记忆系统 (Memory)** 三个独立模块的叙事引擎。

## 架构

```
用户输入
  │
  ▼
┌─────────────────────────────────────┐
│  主编排器 (BookEngine)               │
│                                     │
│  ① Writer   → 生成叙事正文           │
│     - 精简作家 prompt（无格式指令）    │
│     - 只接收剧情上下文 + 记忆摘要     │
│                                     │
│  ② Executor → 信息抽取 + 卡片更新    │
│     - Extractor: 从正文提取结构化数据  │
│     - CardManager: 更新玩家/NPC/道具  │
│     - Formatter: 渲染为 HTML 输出     │
│                                     │
│  ③ Memory   → SQLite 持久化记忆      │
│     - 临时记忆 (10条) → 永久记忆      │
│     - 永久记忆 (6条) → 压缩 (3条)     │
│     - 确定性整合算法 + LLM 辅助       │
└─────────────────────────────────────┘
  │
  ▼
完整输出（正文 + 信息卡 + 记忆区）
```

## 与原提示词的关键区别

| 问题 | 原提示词 | 本项目 |
|------|----------|--------|
| 记忆可靠性 | LLM 上下文窗口记忆，长对话必然丢失 | SQLite 持久化，永不过期 |
| 身份冲突 | 作家 vs 执行者指令在同一 prompt 打架 | 拆分为独立模块，各司其职 |
| 上下文污染 | 信息卡元数据占用大量 token | Writer 只接收精简摘要 |
| 信息卡一致性 | 依赖 LLM "记得"更新每张卡 | Extractor 后处理管道，确定性更新 |

## 快速开始

### PowerShell

```powershell
# 进入项目目录
cd "D:\Python Project\Book"

# 初始化（创建数据库）
uv run python -m book.main init

# 查看当前状态
uv run python -m book.main state

# 交互模式
uv run python -m book.main interactive

# 单轮执行
uv run python -m book.main run -i "玩家行动描述" -o output.md
```

### Git Bash / MSYS

```bash
cd "/d/Python Project/Book"
uv run python -m book.main init
uv run python -m book.main state
uv run python -m book.main interactive
uv run python -m book.main run -i "玩家行动描述" -o output.md
```

### 配置 API Key

**方式 1：环境变量（推荐）**

```powershell
# PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."

# Git Bash
export DEEPSEEK_API_KEY="sk-..."
```

**方式 2：交互式输入**

启动 `run` 或 `interactive` 模式时，如果未检测到环境变量，程序会自动提示输入（输入不回显）。

## 项目结构

```
src/book/
├── main.py              # 主编排器 + CLI
├── config.py            # 配置集中管理
├── models.py            # Pydantic 数据模型
├── db/
│   ├── schema.py        # SQLite 表结构
│   └── repository.py    # 数据访问层
├── memory/
│   ├── manager.py       # 记忆生命周期管理
│   └── integrator.py    # 整合/压缩提示词
├── writer/
│   ├── prompt.py        # 作家系统提示词
│   └── generator.py     # LLM 叙事生成
├── executor/
│   ├── extractor.py     # 结构化信息抽取
│   ├── card_manager.py  # 信息卡更新
│   └── formatter.py     # 输出格式化
└── cards/               # (预留) 各卡片模块
```

## 代码审查历史

本项目经过两轮代码审查：
1. **自查**（作者审查）— 发现 6 项问题并修复
2. **独立 agent 审查** — 发现 7 项新问题（事务原子性、字段漏校验等），全部修复
