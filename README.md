# Book 叙事引擎

把原始单体 AI 提示词拆分为 **作家 (Writer)**、**系统执行者 (Executor)**、**记忆系统 (Memory)**
三个独立模块的叙事引擎，并在此之上提供**可承载 800 万字长篇的叙事状态服务**：

- **检索层**：中文二元组分词 + SQLite FTS5 全文索引，外加零依赖的稀疏向量语义召回
- **结构层级**：卷 / 章 / 场景 / POV / 伏笔 / 实体别名
- **提及驱动注入**：只把 POV、被提及、在场的实体喂给 Writer，受字符预算硬约束
- **一致性检查**：时间线 / 状态 / 伏笔三类规则，可选用 LLM 深度比对
- **两种消费模式，一个状态引擎**：互动模式（回合制）与连载模式（卷章写作）
- **多书隔离**：每本书一个数据库文件
- **HTTP 服务**：给 Android / EIAI 等外部客户端用的 REST 接口

## 架构

```
BookEngine（src/book/main.py，主编排器）
 ├─ Writer     src/book/writer/{prompt.py, generator.py, context_builder.py}
 │             提及驱动上下文（预算约束）+ 精简人设 prompt → LLM 生成正文
 ├─ Executor   src/book/executor/{extractor.py, card_manager.py, formatter.py, consistency.py}
 │             Extractor：LLM 从正文抽 JSON → CardManager：写卡片表 → 一致性检查
 ├─ Memory     src/book/memory/{manager.py, integrator.py, vector.py}
 │             临时记忆(10条) → 永久记忆 → 压缩 → 剧情摘要；稀疏向量检索
 ├─ DB         src/book/db/{schema.py, repository.py, migrations.py, search.py}
 │             建表 / 数据访问 / 幂等迁移 / FTS5 检索层
 └─ 接口层     src/book/cli.py（bookctl）/ web_server.py（HTTP）
```

## 快速开始

需要 Python 3.11+。本项目**零重依赖**（只用 stdlib + httpx + pydantic + rich）。

```bash
# 建虚拟环境并安装（可编辑安装，bookctl 会进 .venv/bin）
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 初始化数据库（默认 data/book.db）
.venv/bin/python -m book.main init

# 查看当前状态
.venv/bin/python -m book.main state

# 交互模式（回合制）
.venv/bin/python -m book.main interactive

# 单轮执行
.venv/bin/python -m book.main run -i "玩家行动描述" -o output.md
```

> 若已安装 [uv](https://docs.astral.sh/uv/)，把上面的 `.venv/bin/python` 换成 `uv run python` 亦可。

### 配置 API Key

```bash
export DEEPSEEK_API_KEY="sk-..."      # 推荐
```
未设置时也能跑：`ingest` 仍会入库正文并建索引，只是跳过结构化抽取（会在 `warnings` 里说明）。
交互模式下若未检测到环境变量，程序会提示输入。

## 命令行（bookctl）

所有子命令都支持 `--json`，输出**单行 JSON**，便于 agent 解析。
退出码：成功 `0`；参数错误 `2`；内部错误 `1`；JSON 模式下错误也是 JSON（`{"error","kind"}`）。

| 命令 | 说明 |
|---|---|
| `bookctl search <query> [--k N] [--source S] [--volume V] [--semantic] [--min-score F]` | 检索历史原文（FTS 或稀疏向量） |
| `bookctl context --pov X [--scene S] [--chapter N] [--budget N]` | 构建写作上下文包（受预算约束） |
| `bookctl ingest --file F \| --text T [--volume V] [--chapter N] [--pov P] [--round R] [--no-extract]` | 把一章正文入库并抽取（**幂等**，按内容 sha256 去重） |
| `bookctl extract [--range A-B] [--force]` | 批量补抽取（默认只处理未抽取过的轮次） |
| `bookctl check [--range A-B] [--foreshadow-stale N] [--deep]` | 一致性检查 |
| `bookctl reindex` | 全量重建检索索引 |
| `bookctl chapter list\|add` | 卷/章管理 |
| `bookctl foreshadow add\|list\|close` | 伏笔管理 |
| `bookctl alias add\|list` | 实体别名（供提及匹配） |
| `bookctl books list\|add <slug>` | 多书管理 |

全局参数：`--book <slug>`（选书，优先于 `--db`）、`--db <path>`、`--no-migrate`（只建表不迁移）。

```bash
# 检索
bookctl search "跳蛋 数据" --k 5 --semantic --json
# {"query": "…", "mode": "semantic", "hits": [{"id":1,"source":"narrative","ref_id":"12",
#   "volume":1,"chapter":12,"pov":"林越","text":"…","snippet":"…","score":0.52,"matched":"semantic"}]}

# 入库（幂等）
bookctl ingest --file "story/某书/第13章.md" --volume 2 --chapter 13 --pov "林越" --json
# {"status":"ingested","round":13,"content_hash":"…","changes":{…},
#  "indexed":{"narrative":1,"entities":3},"warnings":[]}

# 一致性检查
bookctl check --range 10-20 --json
# {"range":[10,20],"issues":[{"kind":"timeline","severity":"high","detail":"…",
#   "refs":[{"table":"session_history","id":12}]}]}
```

## HTTP API

```bash
.venv/bin/book-web                       # 默认 127.0.0.1:8080，自动开浏览器
.venv/bin/python -m book.web_server --host 127.0.0.1 --port 8080 --no-browser
```

> **安全**：绑定到非 `127.0.0.1`/`::1`/`localhost` 时**必须**设置 `BOOK_API_TOKEN`，
> 否则拒绝启动。设置后所有 `/api/*` 请求需带 `Authorization: Bearer <token>`
> （或 `X-API-Token: <token>`）。静态页面 `/` 不需要令牌，方便浏览器直接打开。

### 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 内嵌交互页面 |
| `GET` | `/api/state` | 当前状态文本 |
| `GET` | `/api/settings`、`POST` `/api/settings` | 读取/保存故事设定 |
| `GET` | `/api/key-status`、`POST` `/api/set-key` | API Key 状态/设置 |
| `GET` | `/api/history` | 全部轮次 |
| `POST` | `/api/chat` | **SSE 流式**一轮（`thinking`/`chunk`/`done` 事件） |
| `POST` | `/api/reset` | 重置当前书 |
| `GET` | `/api/books` | 书列表 |
| `POST` | `/api/context` | body `{pov, scene, chapter, budget_chars, extra_text}` → ContextPack |
| `POST` | `/api/ingest` | body `{file_path?, text?, volume?, chapter?, pov?, round?, no_extract?}` |
| `GET` | `/api/search?q=&k=&source=&volume=&semantic=&min_score=` | 检索 |
| `GET` | `/api/check?range=&foreshadow_stale=` | 一致性检查 |

响应字段与 `bookctl --json` **同名同类型**。错误统一为 `{"error": "…", "kind": "usage|not_found|unauthorized|internal"}`。

### 选书

三种方式（任选其一）：查询串 `?book=<slug>`、请求头 `X-Book: <slug>`、请求体 `{"book": "<slug>"}`。
HTTP 层用 `ThreadingHTTPServer`，SSE 期间不会阻塞其它请求。

```bash
# 检索示例
curl -s "http://127.0.0.1:8080/api/search?q=%E6%95%B0%E6%8D%AE&k=5" | jq .

# 入库示例（正文直接传）
curl -s -X POST http://127.0.0.1:8080/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text":"林越走进服务器机房。","chapter":13,"pov":"林越"}'

# 指定书 + 令牌
curl -s -H "Authorization: Bearer $BOOK_API_TOKEN" \
  "http://127.0.0.1:8080/api/context?book=alpha" \
  -X POST -d '{"pov":"林越","budget_chars":6000}'
```

### SSE 事件格式（`POST /api/chat`）

`Content-Type: text/event-stream`，每条事件是一行 `data: {json}`：

```
data: {"type":"thinking","content":"…"}     # 模型的思考过程（可选）
data: {"type":"chunk","content":"…"}        # 正文增量
data: {"type":"done","output":"…"}          # 完成，output 为完整输出
data: {"type":"error","message":"…"}        # 出错（可能出现在流中间）
```

### Android / EIAI 接入

1. 在电脑上启动服务：`book-web --host 0.0.0.0 --port 8080`
   （**必须**同时 `export BOOK_API_TOKEN=...`，否则拒绝启动）
2. 手机与电脑同一局域网，base URL 用电脑内网 IP：`http://192.168.x.x:8080`
3. 所有请求带 `Authorization: Bearer <BOOK_API_TOKEN>`
4. 无 SSE 需求的客户端只用 `/api/search`、`/api/context`、`/api/ingest`、`/api/check` 即可，
   它们都是普通 JSON 请求/响应，适合 OkHttp/Retrofit 直接映射

Kotlin（OkHttp）最小示例：

```kotlin
val client = OkHttpClient()
val base = "http://192.168.1.10:8080"
val token = "你的 BOOK_API_TOKEN"

fun search(q: String): String {
    val url = "$base/api/search?q=" + URLEncoder.encode(q, "UTF-8")
    val req = Request.Builder().url(url)
        .header("Authorization", "Bearer $token")
        .build()
    return client.newCall(req).execute().body!!.string()
}

// SSE 流式一轮
fun chat(input: String, onChunk: (String) -> Unit) {
    val body = """{"input": ${JSONObject.quote(input)}}"""
        .toRequestBody("application/json".toMediaType())
    val req = Request.Builder().url("$base/api/chat")
        .header("Authorization", "Bearer $token").post(body).build()
    client.newCall(req).execute().body!!.source().use { src ->
        while (!src.exhausted()) {
            val line = src.readUtf8Line() ?: break
            if (line.startsWith("data: ")) onChunk(line.removePrefix("data: "))
        }
    }
}
```

## 多书隔离

```
data/
├── books.json          # 注册表：slug / 书名 / 创建时间 / 库路径
└── books/
    ├── default.db      # 由旧的 data/book.db 复制而来（原文件保留作备份）
    └── alpha.db
```

每本书一个独立 SQLite 文件，**互不干扰**（已验证：A 书写入后，默认书检索不到 A 的内容，反之亦然）。

```bash
bookctl books add alpha --title "阿尔法之书"
bookctl --book alpha ingest --file 第一章.md --chapter 1 --json
bookctl --book alpha search "关键词" --json
```

## 测试

```bash
for t in test_runner test_p0 test_p1 test_p2 test_p3; do
  .venv/bin/python check/$t.py
done
```

| 套件 | 覆盖 |
|---|---|
| `check/test_runner.py` | 既有基础逻辑（配置/模型/CRUD/记忆/抽取/格式化/事务） |
| `check/test_p0.py` | 中文分词、FTS5 检索、幂等迁移、CLI 契约、ingest 幂等 |
| `check/test_p1.py` | 卷章场景、伏笔、别名、提及驱动注入、预算 |
| `check/test_p2.py` | 稀疏向量、语义检索、三类一致性检查、成本优化 |
| `check/test_p3.py` | 多书隔离、HTTP 端点、并发、令牌、--no-migrate |

全部测试**不需要 API Key、不联网**（LLM 路径一律 mock）。

## 项目结构

```
src/book/
├── main.py              # 主编排器 BookEngine + 旧 CLI
├── cli.py               # bookctl（给 agent 与人类共用的接口）
├── web_server.py        # HTTP 服务（ThreadingHTTPServer）
├── books.py             # 多书隔离与注册表
├── config.py            # 配置集中管理
├── models.py            # Pydantic 数据模型
├── db/
│   ├── schema.py        # 表结构（BASE_SCHEMA_SQL + P1_SCHEMA_SQL）
│   ├── repository.py    # 数据访问层（所有 SQL）
│   ├── migrations.py    # 幂等迁移
│   └── search.py        # 分词 + FTS5 + 稀疏向量检索
├── memory/
│   ├── manager.py       # 记忆生命周期
│   ├── integrator.py    # 整合/压缩提示词
│   └── vector.py        # FNV-1a 稀疏向量 + Embedder 协议
├── writer/
│   ├── prompt.py        # 作家系统提示词
│   ├── context_builder.py  # 提及驱动上下文（预算约束）
│   └── generator.py     # LLM 调用
└── executor/
    ├── extractor.py     # 结构化信息抽取
    ├── card_manager.py  # 信息卡更新
    ├── consistency.py   # 一致性检查
    └── formatter.py     # 输出格式化
```

## 已知边界

- **语义检索**用字符 n-gram 稀疏向量：擅长**共享词汇**的改写（"机房/服务器" ↔ "服务器机房"），
  对**纯同义改写**（"电脑" ↔ "计算机"）召回为 0。要覆盖需接 dense 模型
  （`memory/vector.py` 里的 `Embedder` 协议已预留接口，依赖做成可选 extra）。
- **语义检索**每次对全表计算向量（无缓存）。万篇以内可接受；更大规模建议加向量缓存。
- 一致性检查的"在场"判定按名字出现次数，单字名不参与（会误命中其他角色的名字子串）。
