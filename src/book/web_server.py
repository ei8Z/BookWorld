"""Book 叙事引擎 Web 服务器 —— 通过浏览器与故事连续交互。"""

from __future__ import annotations
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import books
from .cli import CliError
from .executor import consistency
from .main import BookEngine


# ─── 引擎实例（按书 + 按线程隔离） ──────────────────────────────────────────
#
# 关键约束：sqlite3.Connection 默认 check_same_thread=True，**不能跨线程使用**。
# ThreadingHTTPServer 每个请求一个线程，所以引擎（进而连接）必须按线程隔离，
# 否则并发请求会成片报 "SQLite objects created in a thread can only be used in
# that same thread"。这里用 thread-local 缓存：每个线程各自持有若干书的引擎。

_local = threading.local()
_engine_lock = threading.Lock()
_all_engines: list[BookEngine] = []

# 当绑定到非回环地址时必须提供的令牌（P3-3 安全要求）
API_TOKEN_ENV = "BOOK_API_TOKEN"

# 允许免令牌访问的地址（本机）。非回环绑定才强制令牌。
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# 本机请求可以免令牌：内嵌页面跑在浏览器里，不方便注入 Authorization 头。
# 安全模型是"把服务暴露到局域网才需要令牌"，本机访问本来就是可信的。
_ALLOW_LOCAL_WITHOUT_TOKEN = True

# 请求体上限：防止 Content-Length 巨值把工作线程卡住（DoS）
MAX_BODY_BYTES = 32 * 1024 * 1024

# /api/ingest 允许读取的文件后缀：file_path 否则等于一个任意文件读取原语
_INGEST_ALLOWED_SUFFIXES = {".md", ".txt", ".markdown", ".text"}


def get_engine(book: str | None = None) -> BookEngine:
    """取当前线程专属的引擎（同一线程内按书缓存复用）。"""
    key = books.normalize_slug(book or books.DEFAULT_SLUG)
    cache = getattr(_local, "engines", None)
    if cache is None:
        cache = {}
        _local.engines = cache
    engine = cache.get(key)
    if engine is None:
        engine = BookEngine(db_path=books.resolve(key))
        if not engine.llm.api_key:
            stored = engine.repo.get_setting("api_key")
            if stored:
                engine.set_api_key(stored)
        cache[key] = engine
        with _engine_lock:
            _all_engines.append(engine)
    return engine


def close_engines() -> None:
    """关闭已创建的全部引擎（测试与退出时用）。"""
    with _engine_lock:
        engines, _all_engines[:] = list(_all_engines), []
    for engine in engines:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass
    _local.engines = {}


# ─── HTML 页面 ────────────────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Book 叙事引擎</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    background: #1a1a2e; color: #e0e0e0;
    height: 100vh; display: flex; flex-direction: column;
  }

  /* 顶部栏 */
  .header {
    background: #16213e; padding: 12px 20px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid #0f3460; flex-shrink: 0;
    position: relative; z-index: 10;
  }
  .header h1 { font-size: 18px; color: #e94560; }
  .header .status { font-size: 13px; color: #888; }
  .header .status.ok { color: #4ecca3; }

  .header-actions { display: flex; gap: 10px; align-items: center; }
  .btn {
    background: #0f3460; color: #e0e0e0; border: 1px solid #1a508b;
    padding: 6px 14px; border-radius: 4px; cursor: pointer; font-size: 13px;
  }
  .btn:hover { background: #1a508b; }
  .btn-danger { background: #6b2020; border-color: #8b3030; }
  .btn-danger:hover { background: #8b3030; }
  .btn-settings { background: #2a4a2a; border-color: #3a6a3a; }
  .btn-settings:hover { background: #3a6a3a; }

  /* 设置弹窗 */
  .modal-overlay {
    display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.7); z-index: 100; justify-content: center;
    align-items: center;
  }
  .modal-overlay.show { display: flex; }
  .modal {
    background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
    padding: 24px; width: 90%; max-width: 700px; max-height: 80vh;
    overflow-y: auto;
  }
  .modal h2 { color: #e94560; margin-bottom: 16px; font-size: 18px; }
  .modal label { display: block; font-size: 13px; color: #b8d4ff; margin: 12px 0 4px; }
  .modal textarea {
    width: 100%; background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 4px; padding: 8px 12px;
    font-size: 13px; font-family: inherit; resize: vertical; min-height: 80px;
  }
  .modal textarea:focus { outline: none; border-color: #e94560; }
  .modal .btn-row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }

  /* 消息区 */
  .messages {
    flex: 1; overflow-y: auto; padding: 20px;
    display: flex; flex-direction: column; gap: 16px;
  }

  .msg {
    max-width: 85%; animation: fadeIn 0.3s ease;
  }
  .msg.user { align-self: flex-end; }
  .msg.system { align-self: flex-start; width: 100%; }

  .msg.user .bubble {
    background: #0f3460; border: 1px solid #1a508b;
    border-radius: 12px 12px 4px 12px; padding: 10px 16px;
    color: #b8d4ff;
  }

  .msg.system .content {
    background: #16213e; border: 1px solid #1a3a5c;
    border-radius: 4px 12px 12px 12px; padding: 16px 20px;
    line-height: 1.8; white-space: pre-wrap; word-break: break-word;
  }
  .msg.system .thinking-box {
    background: #1a1a2e; border: 1px solid #0f3460;
    border-radius: 4px; padding: 8px 12px; margin-bottom: 8px;
  }
  .msg.system .thinking-box summary {
    cursor: pointer; color: #e94560; font-weight: bold;
    padding: 4px 0; user-select: none;
  }

  .msg.system .content details {
    margin: 10px 0; background: #1a1a2e; border: 1px solid #0f3460;
    border-radius: 4px; padding: 8px 12px;
  }
  .msg.system .content details summary {
    cursor: pointer; color: #e94560; font-weight: bold;
    padding: 4px 0; user-select: none;
  }
  .msg.system .content details table {
    width: 100%; border-collapse: collapse; margin: 8px 0;
    font-size: 13px;
  }
  .msg.system .content details th,
  .msg.system .content details td {
    border: 1px solid #1a3a5c; padding: 4px 8px; text-align: left;
  }
  .msg.system .content details th {
    background: #0f3460; color: #b8d4ff;
  }

  .msg .meta {
    font-size: 11px; color: #666; margin-bottom: 4px;
  }

  /* 输入区 */
  .input-area {
    background: #16213e; padding: 14px 20px;
    border-top: 1px solid #0f3460; flex-shrink: 0;
    display: flex; gap: 10px;
  }
  .input-area textarea {
    flex: 1; background: #1a1a2e; color: #e0e0e0;
    border: 1px solid #0f3460; border-radius: 8px;
    padding: 10px 14px; font-size: 14px; resize: none;
    font-family: inherit; min-height: 44px; max-height: 120px;
  }
  .input-area textarea:focus {
    outline: none; border-color: #e94560;
  }
  .input-area button {
    background: #e94560; color: #fff; border: none;
    padding: 10px 24px; border-radius: 8px; cursor: pointer;
    font-size: 14px; font-weight: bold; white-space: nowrap;
  }
  .input-area button:hover { background: #c73a52; }
  .input-area button:disabled { background: #555; cursor: not-allowed; }

  /* 加载动画 */
  .loading {
    display: flex; align-items: center; gap: 8px;
    color: #888; font-size: 13px; padding: 8px 0;
  }
  .spinner {
    width: 16px; height: 16px; border: 2px solid #1a3a5c;
    border-top-color: #e94560; border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); }
                        to { opacity: 1; transform: translateY(0); } }

  /* 滚动条 */
  .messages::-webkit-scrollbar { width: 6px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb { background: #1a3a5c; border-radius: 3px; }

  .empty-state {
    text-align: center; color: #555; margin-top: 80px;
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
  .empty-state p { font-size: 14px; line-height: 1.8; }

  /* 响应式 */
  @media (max-width: 600px) {
    .msg { max-width: 95%; }
    .header h1 { font-size: 15px; }
    .input-area { padding: 10px; gap: 6px; }
    .input-area button { padding: 10px 14px; font-size: 13px; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Book 叙事引擎</h1>
    <span class="status" id="status">就绪</span>
  </div>
  <div class="header-actions">
    <button class="btn btn-settings" onclick="openSettings()" title="编辑故事设定">⚙ 设定</button>
    <button class="btn" onclick="viewState()" title="查看当前完整状态">查看状态</button>
    <button class="btn btn-danger" onclick="resetConfirm()" title="重置所有数据">重置剧情</button>
  </div>
</div>

<div class="messages" id="messages">
  <div class="empty-state" id="empty">
    <div class="icon">📖</div>
    <p>欢迎使用 Book 叙事引擎<br>输入玩家行动，开始你的故事。</p>
    <p style="margin-top:10px;font-size:12px;color:#444;">
      例：我推开旧城区废弃仓库的铁门，手电筒的光扫过堆积的灰尘…
    </p>
  </div>
</div>

<div class="modal-overlay" id="stateModal">
  <div class="modal">
    <h2>📊 当前状态</h2>
    <div id="stateContent" style="line-height:1.8;max-height:60vh;overflow-y:auto;white-space:pre-wrap;word-break:break-word;font-size:13px;">加载中…</div>
    <div class="btn-row">
      <button class="btn" onclick="closeState()">关闭</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="settingsModal">
  <div class="modal">
    <h2>⚙ 故事设定</h2>
    <p style="font-size:12px;color:#666;margin-bottom:12px;">修改后将影响后续所有叙事生成。保存后即刻生效。</p>
    <label>主角档案（player_profile）</label>
    <textarea id="s_player_profile" rows="3" placeholder="姓名、年龄、体型、隐秘特质等"></textarea>
    <label>主角能力细则（player_abilities）</label>
    <textarea id="s_player_abilities" rows="6" placeholder="变形者能力的详细规则"></textarea>
    <label>世界背景（world_setting）</label>
    <textarea id="s_world_setting" rows="3" placeholder="时代、世界观核心规则"></textarea>
    <label>故事起点（initial_setup）</label>
    <textarea id="s_initial_setup" rows="3" placeholder="故事开端的场景和氛围设定"></textarea>
    
    <label>API Key（留空则使用环境变量 DEEPSEEK_API_KEY）</label>
    <div style="display:flex;gap:8px;">
      <input type="password" id="s_api_key" style="flex:1;background:#1a1a2e;color:#e0e0e0;border:1px solid #0f3460;border-radius:4px;padding:8px 12px;font-size:13px;font-family:inherit;" placeholder="sk-...">
      <button class="btn" onclick="toggleKeyVisibility()" title="显示/隐藏">显示</button>
    </div>
<div class="btn-row">
      <button class="btn" onclick="closeSettings()">取消</button>
      <button class="btn" onclick="saveSettings()" style="background:#4ecca3;border-color:#3a8a6a;color:#1a1a2e;">💾 保存设定</button>
    </div>
  </div>
</div>

<div class="input-area">
  <textarea id="input" rows="2"
    placeholder="输入玩家行动…"
    onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send();}">
  </textarea>
  <button id="sendBtn" onclick="send()">发送</button>
</div>

<script>
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const statusEl = document.getElementById('status');
const emptyEl = document.getElementById('empty');
let isLoading = false;

function setStatus(text, ok) {
  statusEl.textContent = text;
  statusEl.className = 'status' + (ok ? ' ok' : '');
}

function hideEmpty() {
  if (emptyEl) emptyEl.style.display = 'none';
}

async function send() {
  const text = inputEl.value.trim();
  if (!text || isLoading) return;
  isLoading = true;
  inputEl.value = '';
  sendBtn.disabled = true;
  setStatus('生成中…');

  hideEmpty();
  addMessage('user', text);
  const loadId = addLoading();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({input: text}),
    });

    if (!resp.ok) {
      removeLoading(loadId);
      setStatus('错误', false);
      addMessage('system', '请求失败：' + resp.status);
      isLoading = false;
      sendBtn.disabled = false;
      inputEl.focus();
      return;
    }

    removeLoading(loadId);
    // 创建流式消息容器
    const streamDiv = document.createElement('div');
    streamDiv.className = 'msg system';
    const contentDiv = document.createElement('div');
    contentDiv.className = 'content';
    // 推理过程折叠区
    let thinkingDiv = null;
    let thinkingSummary = null;
    let thinkingText = '';
    streamDiv.appendChild(contentDiv);
    messagesEl.appendChild(streamDiv);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const STREAM_TIMEOUT = 180000; // 3 分钟总超时
    const startTime = Date.now();
    let streamError = null;

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;

      if (Date.now() - startTime > STREAM_TIMEOUT) {
        streamError = '响应超时，请重试';
        reader.cancel();
        break;
      }

      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split('\\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            if (event.type === 'thinking') {
              if (!thinkingDiv) {
                thinkingDiv = document.createElement('details');
                thinkingDiv.className = 'thinking-box';
                thinkingDiv.open = true;
                thinkingSummary = document.createElement('summary');
                thinkingSummary.textContent = '思考中…';
                thinkingDiv.appendChild(thinkingSummary);
                streamDiv.insertBefore(thinkingDiv, contentDiv);
              }
              thinkingText += event.content;
              thinkingSummary.textContent = '思考中… (' + thinkingText.length + ' 字)';
              messagesEl.scrollTop = messagesEl.scrollHeight;
            } else if (event.type === 'chunk') {
              if (thinkingDiv && thinkingDiv.open) {
                thinkingDiv.open = false;
                thinkingSummary.textContent = '思考过程 (' + thinkingText.length + ' 字)';
              }
              contentDiv.textContent += event.content;
              messagesEl.scrollTop = messagesEl.scrollHeight;
            } else if (event.type === 'done') {
              // 将思考内容写入折叠区
              if (thinkingDiv && thinkingText) {
                const pre = document.createElement('pre');
                pre.textContent = thinkingText;
                pre.style.cssText = 'white-space:pre-wrap;font-size:0.8em;opacity:0.7;max-height:200px;overflow-y:auto;';
                thinkingDiv.appendChild(pre);
              }
              contentDiv.innerHTML = sanitizeHtml(event.output || contentDiv.textContent);
              messagesEl.scrollTop = messagesEl.scrollHeight;
              if (event.output && event.output.includes('[未配置 API Key')) {
                setTimeout(() => openSettings(), 500);
              }
            } else if (event.type === 'error') {
              contentDiv.textContent = '错误：' + (event.message || '未知错误');
              setStatus('错误', false);
            }
          } catch (e) {
            // 忽略 JSON 解析错误
          }
        }
      }
    }

    if (streamError) {
      contentDiv.textContent = streamError;
      setStatus('超时', false);
    } else {
      setStatus('就绪', true);
    }
  } catch (e) {
    removeLoading(loadId);
    setStatus('连接失败', false);
    addMessage('system', '连接服务器失败：' + e.message);
  }

  isLoading = false;
  sendBtn.disabled = false;
  inputEl.focus();
}

async function viewState() {
  document.getElementById('stateContent').textContent = '加载中…';
  document.getElementById('stateModal').classList.add('show');
  try {
    const resp = await fetch('/api/state');
    const data = await resp.json();
    if (data.error) {
      document.getElementById('stateContent').textContent = '读取状态失败：' + data.error;
    } else {
      document.getElementById('stateContent').innerHTML = sanitizeHtml(data.output || '（无法读取状态）');
    }
  } catch (e) {
    document.getElementById('stateContent').textContent = '获取状态失败：' + e.message;
  }
}

function closeState() {
  document.getElementById('stateModal').classList.remove('show');
}

document.getElementById('stateModal').addEventListener('click', function(e) {
  if (e.target === this) closeState();
});

async function resetConfirm() {
  if (!confirm('确定要重置所有剧情数据吗？此操作不可撤销！')) return;
  try {
    const resp = await fetch('/api/reset', {method: 'POST'});
    const data = await resp.json();
    messagesEl.innerHTML = '';
    if (emptyEl) {
      emptyEl.style.display = '';
      messagesEl.appendChild(emptyEl);
    }
    setStatus('已重置', true);
  } catch (e) {
    setStatus('重置失败', false);
  }
}

function addMessage(type, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + type;
  if (type === 'user') {
    div.innerHTML = '<div class="bubble">' + escapeHtml(text) + '</div>';
  } else {
    div.innerHTML = '<div class="content">' + sanitizeHtml(text) + '</div>';
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function addLoading() {
  const div = document.createElement('div');
  div.className = 'loading';
  div.id = 'load-' + Date.now();
  div.innerHTML = '<div class="spinner"></div> 正在生成叙事…';
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div.id;
}

function removeLoading(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function sanitizeHtml(html) {
  // Use DOMParser to safely parse and strip dangerous elements
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  // Remove all script tags
  doc.querySelectorAll('script').forEach(el => el.remove());
  // Remove on* event handlers from all elements
  doc.querySelectorAll('*').forEach(el => {
    for (const attr of [...el.attributes]) {
      if (attr.name.startsWith('on')) {
        el.removeAttribute(attr.name);
      }
    }
  });
  return doc.body.innerHTML;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// 启动时加载对话历史
fetch('/api/history')
  .then(r => r.json())
  .then(d => {
    if (d.rounds && d.rounds.length > 0) {
      hideEmpty();
      setStatus('已有存档', true);
      d.rounds.forEach(r => {
        addMessage('user', r.user_input);
        addMessage('system', r.narrative);
      });
    }
  })
  .catch(() => {});

// 检查 API Key 是否已配置，没有则自动弹出设置
fetch('/api/key-status')
  .then(r => r.json())
  .then(d => {
    if (!d.has_key) {
      setStatus('未配置 API Key，请在设定中填入', false);
      setTimeout(() => openSettings(), 300);
    }
  })
  .catch(() => {});

inputEl.focus();

// ─── 设置弹窗 ──────────────────────────────────────────────────────────

async function openSettings() {
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'read'}),
    });
    const data = await resp.json();
    const s = data.settings || {};
    document.getElementById('s_player_profile').value = s.player_profile || '';
    document.getElementById('s_player_abilities').value = s.player_abilities || '';
    document.getElementById('s_world_setting').value = s.world_setting || '';
    document.getElementById('s_initial_setup').value = s.initial_setup || '';
    document.getElementById('settingsModal').classList.add('show');
  } catch (e) {
    alert('读取设定失败：' + e.message);
  }
}

function closeSettings() {
  document.getElementById('settingsModal').classList.remove('show');
}

async function saveSettings() {
  const settings = {
    player_profile: document.getElementById('s_player_profile').value,
    player_abilities: document.getElementById('s_player_abilities').value,
    world_setting: document.getElementById('s_world_setting').value,
    initial_setup: document.getElementById('s_initial_setup').value,
  };
  // Save API key separately if provided
  const apiKey = document.getElementById('s_api_key').value.trim();
  if (apiKey) {
    await fetch('/api/set-key', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: apiKey}),
    });
    document.getElementById('s_api_key').value = '';
  }
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save', settings: settings}),
    });
    const data = await resp.json();
    if (data.ok) {
      closeSettings();
      setStatus('设定已保存', true);
    } else {
      alert('保存失败：' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('保存失败：' + e.message);
  }
}

// 点击遮罩关闭
function toggleKeyVisibility() {
  const inp = document.getElementById('s_api_key');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

document.getElementById('settingsModal').addEventListener('click', function(e) {
  if (e.target === this) closeSettings();
});
</script>
</body>
</html>"""


# ─── HTTP 请求处理器 ──────────────────────────────────────────────────────────

class BookHandler(BaseHTTPRequestHandler):
    # 由 run_server 注入：绑定非回环地址时要求的令牌（None 表示不要求）
    required_token: str | None = None

    def log_message(self, format, *args):
        """重定向日志到 stderr。"""
        print(f"[Web] {args[0]}", file=sys.stderr)

    # ─── 请求解析 ─────────────────────────────────────────────────────────

    @property
    def _parsed(self):
        return urlparse(self.path)

    @property
    def route(self) -> str:
        return self._parsed.path

    @property
    def query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(self._parsed.query).items()}

    def _book(self) -> str | None:
        """从 ?book= / X-Book 头取书标识。"""
        book = self.query.get("book") or self.headers.get("X-Book")
        return book.strip() if book else None

    def _is_local(self) -> bool:
        """请求是否来自本机。"""
        host = (self.client_address[0] if self.client_address else "") or ""
        return host in _LOOPBACK

    def _authorized(self) -> bool:
        if not self.required_token:
            return True
        # 本机（浏览器 UI）免令牌：令牌是为了防止局域网/公网裸奔
        if _ALLOW_LOCAL_WITHOUT_TOKEN and self._is_local():
            return True
        header = self.headers.get("Authorization", "") or ""
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            if parts[1].strip() == self.required_token:
                return True
        token_header = (self.headers.get("X-API-Token", "") or "").strip()
        return token_header == self.required_token

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, kind: str = "internal", status: int = 500):
        """错误体与 CLI 的 JSON 错误保持同形：{"error","kind"}。"""
        self._send_json({"error": message, "kind": kind}, status)

    # ─── 参数校验（与 CLI 的口径保持一致：非法输入 → 400 usage） ──────────

    def _int_param(self, value, name: str, default=None, minimum=None, maximum=None):
        """把 HTTP 参数强转成 int；非法时抛 CliError（由调用方转成 400）。

        不接受 bool（JSON true 会被 int() 变成 1，属于静默错值）。
        """
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise CliError(f"{name} 必须是整数", kind="usage", code=2)
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise CliError(f"{name} 必须是整数，收到 {value!r}", kind="usage", code=2)
        if minimum is not None and num < minimum:
            raise CliError(f"{name} 不能小于 {minimum}，收到 {num}", kind="usage", code=2)
        if maximum is not None and num > maximum:
            raise CliError(f"{name} 不能大于 {maximum}，收到 {num}", kind="usage", code=2)
        return num

    def _float_param(self, value, name: str, default=None):
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise CliError(f"{name} 必须是数字", kind="usage", code=2)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise CliError(f"{name} 必须是数字，收到 {value!r}", kind="usage", code=2)

    def _str_param(self, value, name: str, default=None):
        """必须是字符串（或 None）。数字/布尔一律拒绝，避免静默强转。"""
        if value is None:
            return default
        if not isinstance(value, str):
            raise CliError(f"{name} 必须是字符串，收到 {type(value).__name__}",
                           kind="usage", code=2)
        return value

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.route

        if route in ("/", "/index.html"):
            self._send_html(HTML_PAGE)
            return

        if not self._authorized():
            self._send_error_json("缺少或错误的访问令牌", "unauthorized", 401)
            return

        try:
            if route == "/api/state":
                engine = get_engine(self._book())
                self._send_json({"output": engine.show_state()})
            elif route == "/api/settings":
                engine = get_engine(self._book())
                self._send_json({"settings": engine.repo.get_all_settings()})
            elif route == "/api/key-status":
                engine = get_engine(self._book())
                stored_key = engine.repo.get_setting("api_key")
                self._send_json({"has_key": bool(engine.llm.api_key or stored_key)})
            elif route == "/api/history":
                engine = get_engine(self._book())
                self._send_json({"rounds": engine.repo.get_session_rounds(limit=0)})
            elif route == "/api/books":
                self._send_json({"books": books.list_books()})
            elif route == "/api/search":
                self._handle_search()
            elif route == "/api/check":
                self._handle_check()
            else:
                self._send_error_json("未知路径", "not_found", 404)
        except books.BookError as e:
            self._send_error_json(str(e), "usage", 400)
        except Exception as e:  # noqa: BLE001
            self._send_error_json(f"{type(e).__name__}: {e}", "internal", 500)

    # ─── P3 新端点（字段与 CLI 的 --json 契约同名） ────────────────────────

    _TRUTHY = {"1", "true", "yes", "on"}

    def _handle_search(self):
        q = self.query
        query = (q.get("q") or "").strip()
        if not query:
            self._send_error_json("缺少查询参数 q", "usage", 400)
            return
        from .db import search as search_mod

        try:
            k = self._int_param(q.get("k"), "k", default=5, minimum=0)
            volume = self._int_param(q.get("volume"), "volume")
            min_score = self._float_param(q.get("min_score"), "min_score")
            source = self._str_param(q.get("source"), "source")
        except CliError as exc:
            self._send_error_json(str(exc), exc.kind, 400)
            return

        semantic = (q.get("semantic") or "").strip().lower() in self._TRUTHY
        engine = get_engine(self._book())
        hits = search_mod.search(
            engine.repo.conn, query, k=k,
            source=source or None,
            volume=volume,
            semantic=semantic,
            min_score=min_score or 0.0,
        )
        self._send_json({
            "query": query,
            "mode": "semantic" if semantic else "fts",
            "hits": [h.to_dict() for h in hits],
        })

    def _handle_check(self):
        q = self.query
        from .cli import _parse_range  # 复用 CLI 的区间校验（错误信息与退出码语义一致）

        try:
            # stale 不能为负（与 CLI 一致：CLI 会直接拒绝）
            stale = self._int_param(
                q.get("foreshadow_stale"), "foreshadow_stale", default=30, minimum=0
            )
            start, end = _parse_range(q.get("range"))
        except CliError as exc:
            self._send_error_json(str(exc), exc.kind, 400)
            return
        engine = get_engine(self._book())
        issues = consistency.run_checks(
            engine.repo, range_start=start, range_end=end, stale_after=stale,
        )
        self._send_json({
            "range": [start, end],
            "issues": [i.to_dict() for i in issues],
        })

    def do_POST(self):
        if not self._authorized():
            self._send_error_json("缺少或错误的访问令牌", "unauthorized", 401)
            return

        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            content_len = int(raw_len)
        except ValueError:
            self._send_error_json("Content-Length 非法", "usage", 400)
            return
        if content_len < 0 or content_len > MAX_BODY_BYTES:
            self._send_error_json(
                f"请求体过大或非法（上限 {MAX_BODY_BYTES} 字节）", "usage", 400
            )
            return
        body_raw = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            self._send_error_json("无效的 JSON", "usage", 400)
            return

        if not isinstance(body, dict):
            self._send_error_json("请求体必须是 JSON 对象", "usage", 400)
            return

        # 请求体里的 book 优先，其次查询串/请求头。
        # 类型必须先校验：body["book"] 是数字时 .strip() 会抛 AttributeError，
        # 直接掐断连接（客户端只看到 RemoteDisconnected，违反 §5 错误契约）。
        raw_book = body.get("book")
        if raw_book is not None and not isinstance(raw_book, str):
            self._send_error_json("book 必须是字符串", "usage", 400)
            return
        book = (raw_book or "").strip() or self._book() or None

        try:
            if self.route == "/api/context":
                self._handle_context(body, book)
                return
            if self.route == "/api/ingest":
                self._handle_ingest(body, book)
                return
        except books.BookError as e:
            self._send_error_json(str(e), "usage", 400)
            return
        except Exception as e:  # noqa: BLE001
            self._send_error_json(f"{type(e).__name__}: {e}", "internal", 500)
            return

        if self.route == "/api/chat":
            user_input = body.get("input", "").strip()
            if not user_input:
                self._send_json({"error": "输入不能为空"}, 400)
                return

            headers_sent = False
            try:
                engine = get_engine(book)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                # 必须让连接在响应结束后关闭：发 "keep-alive" 会把 close_connection
                # 置回 False，线程会永远卡在 readline 上（每连接泄漏一个线程，
                # 客户端按 EOF 判断结束的写法也会挂死）。SSE 的结束标志是 done 事件。
                self.close_connection = True
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                headers_sent = True

                for sse_event in engine.run_round_stream(user_input):
                    self.wfile.write(sse_event.encode("utf-8"))
                    self.wfile.flush()
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.stderr)
                if headers_sent:
                    error_event = f"data: {json.dumps({'type': 'error', 'message': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
                    try:
                        self.wfile.write(error_event.encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                else:
                    self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        elif self.route == "/api/reset":
            # 关闭当前线程这本书的引擎，删除数据库，重新初始化（保留 API Key）
            try:
                key = books.normalize_slug(book or books.DEFAULT_SLUG)
                cache = getattr(_local, "engines", {}) or {}
                old = cache.pop(key, None)
                saved_key = ""
                if old is not None:
                    saved_key = old.repo.get_setting("api_key")
                    old.close()
                    with _engine_lock:
                        if old in _all_engines:
                            _all_engines.remove(old)
                target = books.resolve(key)
                if target.exists():
                    target.unlink()
                engine = get_engine(key)
                if saved_key:
                    engine.set_api_key(saved_key)
                    engine.repo.set_setting("api_key", saved_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_error_json(str(e), "internal", 500)

        elif self.route == "/api/settings":
            if body.get("action") == "save":
                # 保存设定
                try:
                    engine = get_engine(book)
                    settings_data = body.get("settings", {})
                    if settings_data:
                        engine.repo.set_settings_batch(settings_data)
                        engine._refresh_writer_prompt()
                    self._send_json({"ok": True})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)
            else:
                # 读取设定
                try:
                    engine = get_engine(book)
                    settings = engine.repo.get_all_settings()
                    self._send_json({"settings": settings})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)

        elif self.route == "/api/set-key":
            api_key = body.get("key", "").strip()
            if not api_key:
                self._send_error_json("Key 不能为空", "usage", 400)
                return
            try:
                engine = get_engine(book)
                engine.set_api_key(api_key)
                engine.repo.set_setting("api_key", api_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_error_json(str(e), "internal", 500)

        else:
            self._send_error_json("未知路径", "not_found", 404)

    # ─── P3 新端点实现 ────────────────────────────────────────────────────

    def _handle_context(self, body: dict, book: str | None):
        from .writer.context_builder import build_context

        try:
            pov = self._str_param(body.get("pov"), "pov", default="") or ""
            scene = self._str_param(body.get("scene"), "scene", default="") or ""
            extra_text = self._str_param(body.get("extra_text"), "extra_text", default="") or ""
            budget = self._int_param(body.get("budget_chars"), "budget_chars", default=6000)
            chapter = self._int_param(body.get("chapter"), "chapter")
        except CliError as exc:
            self._send_error_json(str(exc), exc.kind, 400)
            return

        if not pov.strip():
            self._send_error_json("pov 不能为空", "usage", 400)
            return
        # 负预算会让 build_context 直接丢光所有分区，属于误用
        if budget is None or budget < 0:
            self._send_error_json("budget_chars 不能为负数", "usage", 400)
            return

        engine = get_engine(book)
        pack = build_context(
            engine.repo,
            pov=pov,
            scene=scene,
            chapter=chapter,
            budget_chars=budget,
            extra_text=extra_text,
        )
        self._send_json(pack.to_dict())

    def _handle_ingest(self, body: dict, book: str | None):
        from .cli import CliError, cmd_ingest, build_parser

        file_path = body.get("file_path")
        text = body.get("text")
        if file_path is not None and not isinstance(file_path, str):
            self._send_error_json("file_path 必须是字符串", "usage", 400)
            return
        if text is not None and not isinstance(text, str):
            self._send_error_json("text 必须是字符串", "usage", 400)
            return
        if not file_path and text is None:
            self._send_error_json("必须提供 file_path 或 text", "usage", 400)
            return

        # 远程可调用的 file_path 等于"任意服务端文件读取原语"：
        # 读任意文件 → 入正文库 → 再 /api/search 取回内容。
        # 这里限制后缀；如果你确实要导入其它格式，请走本地 CLI（bookctl ingest）。
        if file_path:
            suffix = Path(file_path).suffix.lower()
            if suffix not in _INGEST_ALLOWED_SUFFIXES:
                self._send_error_json(
                    f"file_path 只允许 {'/'.join(sorted(_INGEST_ALLOWED_SUFFIXES))} "
                    f"后缀（收到 {suffix or '无后缀'}）；其它格式请用本地 CLI 导入",
                    "usage", 400,
                )
                return

        argv = ["--db", str(books.resolve(book)), "ingest", "--json"]
        if book:
            argv = ["--book", book, "ingest", "--json"]
        if body.get("file_path"):
            argv += ["--file", str(body["file_path"])]
        else:
            argv += ["--text", str(body["text"])]
        if body.get("volume") is not None:
            argv += ["--volume", str(body["volume"])]
        if body.get("chapter") is not None:
            argv += ["--chapter", str(body["chapter"])]
        if body.get("pov"):
            argv += ["--pov", str(body["pov"])]
        if body.get("round") is not None:
            argv += ["--round", str(body["round"])]
        if body.get("no_extract"):
            argv += ["--no-extract"]

        args = build_parser().parse_args(argv)
        try:
            payload = cmd_ingest(args)
        except CliError as exc:
            self._send_error_json(str(exc), exc.kind,
                                  400 if exc.kind == "usage" else 500)
            return
        self._send_json(payload)

    def do_OPTIONS(self):
        """处理 CORS 预检请求。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-API-Token, X-Book",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()


# ─── 服务器启动 ───────────────────────────────────────────────────────────────

def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
    book: str | None = None,
):
    """启动 Web 服务器。

    - 用 ThreadingHTTPServer：SSE 期间不再阻塞其它请求（P3-1）
    - 绑定非回环地址时必须设置 BOOK_API_TOKEN，否则拒绝启动（P3-3）
    """
    from .config import config

    # 安全闸门：暴露到 127.0.0.1 之外必须带令牌
    token = os.environ.get(API_TOKEN_ENV, "").strip()
    if host not in _LOOPBACK and not token:
        print(
            f"错误：绑定到 {host}（非回环地址）时必须设置环境变量 {API_TOKEN_ENV}，"
            "否则任何人都能读写你的故事数据。已拒绝启动。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    BookHandler.required_token = token or None

    # 检查 API Key 是否已配置（环境变量 + 数据库）
    has_key = bool(config.llm_api_key)
    if not has_key and config.db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(config.db_path))
            row = conn.execute("SELECT value FROM story_settings WHERE key='api_key'").fetchone()
            conn.close()
            if row and row[0]:
                has_key = True
        except Exception:
            pass

    key_status = ""
    if not has_key:
        key_status = """
║  ⚠ 未检测到 API Key！                        ║
║  请在浏览器页面的「设定」中填入 API Key        ║
║  或设置环境变量 DEEPSEEK_API_KEY              ║
║                                              ║"""

    server = ThreadingHTTPServer((host, port), BookHandler)
    server.daemon_threads = True
    url = f"http://{host}:{port}"

    print(f"""
╔══════════════════════════════════════════════╗
║       Book 叙事引擎 - Web 界面               ║
║                                              ║
║  打开浏览器访问: {url}                  ║{key_status}
║  按 Ctrl+C 停止服务器                        ║
╚══════════════════════════════════════════════╝
""")

    if open_browser:
        import webbrowser
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
        server.shutdown()
        close_engines()


def main(argv: list[str] | None = None) -> int:
    """入口（book-web 脚本与 python -m 共用，会真的解析参数）。"""
    import argparse

    parser = argparse.ArgumentParser(description="Book 叙事引擎 Web 服务器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--book", default=None, help="默认书标识（多书隔离）")
    args = parser.parse_args(argv)
    run_server(host=args.host, port=args.port,
               open_browser=not args.no_browser, book=args.book)
    return 0


if __name__ == "__main__":
    sys.exit(main())
