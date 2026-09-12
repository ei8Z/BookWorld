"""Book 叙事引擎 Web 服务器 —— 通过浏览器与故事连续交互。"""

from __future__ import annotations
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

from .main import BookEngine


# ─── 全局引擎实例（模块级别，整个服务器生命周期共享） ────────────────────────

_engine: BookEngine | None = None


def get_engine() -> BookEngine:
    global _engine
    if _engine is None:
        _engine = BookEngine()
        # 从数据库加载持久化的 API Key（优先环境变量）
        if not _engine.llm.api_key:
            stored = _engine.repo.get_setting("api_key")
            if stored:
                _engine.set_api_key(stored)
    return _engine


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

    def log_message(self, format, *args):
        """重定向日志到 stderr。"""
        print(f"[Web] {args[0]}", file=sys.stderr)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_html(HTML_PAGE)
        elif self.path == "/api/state":
            try:
                engine = get_engine()
                output = engine.show_state()
                self._send_json({"output": output})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/settings":
            try:
                engine = get_engine()
                settings = engine.repo.get_all_settings()
                self._send_json({"settings": settings})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
        elif self.path == "/api/key-status":
            engine = get_engine()
            # 检查内存和数据库中的 key
            stored_key = engine.repo.get_setting("api_key")
            self._send_json({"has_key": bool(engine.llm.api_key or stored_key)})
        elif self.path == "/api/history":
            engine = get_engine()
            rounds = engine.repo.get_session_rounds(limit=0)  # 0 = 全部
            self._send_json({"rounds": rounds})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(body_raw)
        except json.JSONDecodeError:
            self._send_json({"error": "无效的 JSON"}, 400)
            return

        if self.path == "/api/chat":
            user_input = body.get("input", "").strip()
            if not user_input:
                self._send_json({"error": "输入不能为空"}, 400)
                return

            headers_sent = False
            try:
                engine = get_engine()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
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

        elif self.path == "/api/reset":
            # 关闭旧引擎，删除数据库，重新初始化（保留 API Key）
            try:
                global _engine
                saved_key = ""
                if _engine:
                    saved_key = _engine.repo.get_setting("api_key")
                    _engine.close()
                    _engine = None
                from .config import config
                if config.db_path.exists():
                    config.db_path.unlink()
                _engine = BookEngine()
                if saved_key:
                    _engine.set_api_key(saved_key)
                    _engine.repo.set_setting("api_key", saved_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/settings":
            if body.get("action") == "save":
                # 保存设定
                try:
                    engine = get_engine()
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
                    engine = get_engine()
                    settings = engine.repo.get_all_settings()
                    self._send_json({"settings": settings})
                except Exception as e:
                    self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/set-key":
            api_key = body.get("key", "").strip()
            if not api_key:
                self._send_json({"error": "Key 不能为空"}, 400)
                return
            try:
                engine = get_engine()
                engine.set_api_key(api_key)
                engine.repo.set_setting("api_key", api_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        """处理 CORS 预检请求。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ─── 服务器启动 ───────────────────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True):
    """启动 Web 服务器。"""
    from .config import config

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

    server = HTTPServer((host, port), BookHandler)
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
        global _engine
        if _engine:
            _engine.close()
            _engine = None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Book 叙事引擎 Web 服务器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, open_browser=not args.no_browser)
