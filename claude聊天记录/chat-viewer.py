#!/usr/bin/env python3
"""Claude Code 聊天记录可视化工具

用法:
  python chat-viewer.py            启动服务 + 打开浏览器（默认，支持删除）
  python chat-viewer.py --export   导出静态 HTML（只读）
  python chat-viewer.py --delete <session_id>  命令行删除
"""

import json
import sys
import os
import shutil
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROJECTS_DIR = Path.home() / ".claude" / "projects"
PORT = 8899


def _decode_project_path(encoded_name):
    """将 .claude/projects/ 下的编码目录名还原为真实路径

    编码规则: C: -> C-, \\ / -> -
    解码: 首个 C- -> C:, 其余 - -> 路径分隔符
    无损解码仅当原始路径不含 - 字符时成立。
    """
    if encoded_name.startswith('C-'):
        decoded = 'C:' + encoded_name[2:]
    else:
        decoded = encoded_name
    decoded = decoded.replace('-', os.sep)
    return decoded


def _open_claude(project_path, session_id):
    """在新终端窗口中启动 claude --resume"""
    if sys.platform == 'win32':
        # 优先用 Windows Terminal，体验更好
        wt = shutil.which('wt')
        if wt:
            subprocess.Popen(
                ['wt', '-d', project_path, 'cmd', '/k',
                 f'claude --resume {session_id}']
            )
        else:
            # 回退到 cmd（start 第一个引号是窗口标题，用空标题占位）
            subprocess.Popen(
                ['cmd', '/c', 'start', '', 'cmd', '/k',
                 f'cd /d "{project_path}" && claude --resume {session_id}']
            )
    else:
        subprocess.Popen(
            ['open', '-a', 'Terminal', '--', 'bash', '-c',
             f'cd "{project_path}" && claude --resume {session_id}; exec bash']
        )


def load_sessions():
    sessions = []
    for jsonl_path in sorted(PROJECTS_DIR.glob("*/*.jsonl"),
                             key=lambda p: p.stat().st_mtime, reverse=True):
        sid = jsonl_path.stem
        lines = []
        try:
            with open(jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue
        if not lines:
            continue

        first_ts, first_user_msg, u_cnt, a_cnt = None, "", 0, 0
        in_tokens, out_tokens, cache_read, cache_create = 0, 0, 0, 0
        model, model_msgs = "", {}
        seen_msg_ids = set()
        for obj in lines:
            t = obj.get("type")
            ts = obj.get("timestamp")
            if ts and not first_ts:
                first_ts = ts
            if t == "user":
                u_cnt += 1
                content = obj.get("message", {}).get("content", "")
                if isinstance(content, str) and content.strip() and not first_user_msg:
                    first_user_msg = content.strip()[:120]
            elif t == "assistant":
                a_cnt += 1
                msg = obj.get("message", {})
                msg_id = msg.get("id", "")
                msg_model = msg.get("model", "")
                if not model and msg_model:
                    model = msg_model
                if msg_id and msg_id not in seen_msg_ids:
                    seen_msg_ids.add(msg_id)
                    u = msg.get("usage", {})
                    # input_tokens 已含 cache_create + cache_read，不再重复加
                    i_tk = u.get("input_tokens", 0)
                    o_tk = u.get("output_tokens", 0)
                    cr = u.get("cache_read_input_tokens", 0)
                    cc = u.get("cache_creation_input_tokens", 0)
                    in_tokens += i_tk
                    out_tokens += o_tk
                    cache_read += cr
                    cache_create += cc
                    if msg_model:
                        model_msgs[msg_model] = model_msgs.get(msg_model, 0) + 1
        if u_cnt == 0:
            continue

        # 计算花费
        pricing = _load_pricing()
        cost = _calc_cost(in_tokens, out_tokens, cache_read, model_msgs, pricing)
        total_tokens = in_tokens + out_tokens

        chat = _extract_chat(lines)
        encoded_dir = jsonl_path.parent.name
        project_path = _decode_project_path(encoded_dir)
        sessions.append({
            "id": sid,
            "date": first_ts or "",
            "first_message": first_user_msg or "(空)",
            "user_msgs": u_cnt,
            "assistant_msgs": a_cnt,
            "tokens": total_tokens,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cache_read": cache_read,
            "cache_create": cache_create,
            "cost": cost,
            "project_dir": encoded_dir,
            "project_path": project_path,
            "project_exists": os.path.isdir(project_path),
            "model": model,
            "chat": chat,
        })
    return sessions


def _extract_chat(lines):
    messages = []
    cur_msg = None  # merge blocks by message.id
    for obj in lines:
        t = obj.get("type")
        ts = obj.get("timestamp", "")
        if t == "user":
            content = obj.get("message", {}).get("content", "")
            if isinstance(content, str) and content.strip():
                messages.append({"role": "user", "content": content, "time": ts})
        elif t == "assistant":
            msg_id = obj.get("message", {}).get("id", "")
            if cur_msg and cur_msg.get("_msg_id") == msg_id:
                # same message, merge content blocks
                for p in obj.get("message", {}).get("content", []):
                    if p.get("type") == "text":
                        cur_msg["text"] = (cur_msg["text"] + "\n\n" + p["text"]).strip()
                    elif p.get("type") == "thinking":
                        cur_msg["thinking"] = (cur_msg["thinking"] + "\n\n" + p["thinking"]).strip()
            else:
                if cur_msg:
                    messages.append(cur_msg)
                parts = obj.get("message", {}).get("content", [])
                txt, th = [], []
                for p in parts:
                    if p.get("type") == "text":
                        txt.append(p["text"])
                    elif p.get("type") == "thinking":
                        th.append(p["thinking"])
                u = obj.get("message", {}).get("usage", {})
                cur_msg = {
                    "role": "assistant",
                    "_msg_id": msg_id,
                    "text": "\n\n".join(txt),
                    "thinking": "\n\n".join(th) if th else "",
                    "model": obj.get("message", {}).get("model", ""),
                    "input_tokens": u.get("input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                    "time": ts,
                }
    if cur_msg:
        messages.append(cur_msg)
    return messages


PRICING_PATH = Path(__file__).parent / "pricing.json"


def _load_pricing():
    try:
        with open(PRICING_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_price(pricing, model_name):
    """大小写/分隔符不敏感匹配模型定价"""
    if not model_name:
        return {}
    mkey = model_name.split("@")[0].lower().replace("-", "").replace("_", "").replace(".", "")
    for pk, pv in pricing.items():
        if pk.startswith("_"):
            continue
        if pk.lower().replace("-", "").replace("_", "").replace(".", "") == mkey:
            return pv
    return {}


def _calc_cost(input_tokens, output_tokens, cache_read, model_msgs, pricing):
    """加权平均计费：input(扣除缓存) * 输入价 + output * 输出价 + 缓存命中 * 缓存价"""
    if not model_msgs:
        return 0.0
    total_msgs = sum(model_msgs.values())
    w_input = w_output = w_cr = 0.0
    for model, count in model_msgs.items():
        p = _get_price(pricing, model)
        if not p:
            continue
        ratio = count / total_msgs
        w_input += p.get("input", 0) * ratio
        w_output += p.get("output", 0) * ratio
        w_cr += p.get("cache_read", 0) * ratio
    # DeepSeek 的 input_tokens 不含 cache_read，直接计费
    cost = (input_tokens / 1e6 * w_input +
            output_tokens / 1e6 * w_output +
            cache_read / 1e6 * w_cr)
    return round(cost, 4)


def _local_time(ts_str):
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts_str[:16] if len(ts_str) >= 16 else ts_str


def build_data(sessions):
    data = []
    for s in sessions:
        chat = [{
            "r": m["role"],
            "c": m.get("content", ""),
            "tx": m.get("text", ""),
            "th": m.get("thinking", ""),
            "m": m.get("model", ""),
            "in": m.get("input_tokens", 0),
            "out": m.get("output_tokens", 0),
            "t": _local_time(m.get("time", "")),
        } for m in s["chat"]]
        data.append({
            "id": s["id"],
            "date": _local_time(s["date"]),
            "raw_date": s["date"][:10] if s["date"] else "",
            "preview": s["first_message"],
            "uc": s["user_msgs"],
            "ac": s["assistant_msgs"],
            "model": s["model"],
            "pex": s["project_exists"],
            "tokens": s["tokens"],
            "in_tokens": s["in_tokens"],
            "out_tokens": s["out_tokens"],
            "cache_read": s["cache_read"],
            "cost": s["cost"],
            "chat": chat,
        })
    return data


def delete_session(session_id):
    deleted = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl_file = project_dir / f"{session_id}.jsonl"
        if jsonl_file.exists():
            try:
                jsonl_file.unlink()
                deleted.append(str(jsonl_file))
            except Exception as e:
                return False, f"删除失败: {e}"
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        subdir = project_dir / session_id
        if subdir.exists() and subdir.is_dir():
            try:
                shutil.rmtree(subdir)
                deleted.append(str(subdir))
            except Exception as e:
                return False, f"删除目录失败: {e}"
    fh_dir = Path.home() / ".claude" / "file-history" / session_id
    if fh_dir.exists():
        try:
            shutil.rmtree(fh_dir)
            deleted.append(str(fh_dir))
        except Exception:
            pass
    # 清理 history.jsonl 中的记录
    history_path = Path.home() / ".claude" / "history.jsonl"
    if history_path.exists():
        try:
            lines = history_path.read_text("utf-8").strip().split("\n")
            new_lines = []
            removed = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("sessionId") == session_id:
                        removed += 1
                        continue
                except json.JSONDecodeError:
                    pass
                new_lines.append(line)
            if removed > 0:
                history_path.write_text("\n".join(new_lines) + "\n", "utf-8")
                deleted.append(f"{history_path} ({removed} 条)")
        except Exception:
            pass
    if not deleted:
        return False, "未找到该会话的文件"
    return True, f"已删除 {len(deleted)} 项"


# ─── HTML ──────────────────────────────────────────────

HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code 聊天记录</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:#1a1a2e;color:#e0e0e0;display:flex;height:100vh;overflow:hidden}
#sb{width:330px;min-width:330px;background:#16213e;border-right:1px solid #0f3460;display:flex;flex-direction:column}
#sb-hd{padding:18px;border-bottom:1px solid #0f3460}
#sb-hd h2{font-size:17px;color:#e94560;margin-bottom:10px}
#q{width:100%;padding:9px 12px;border:1px solid #0f3460;border-radius:8px;background:#1a1a2e;color:#e0e0e0;font-size:13px;outline:none}
#q:focus{border-color:#e94560}
#tdy{font-size:11px;color:#aaa;margin-bottom:10px;line-height:1.6}
#tdy span{color:#e94560;font-weight:600}
#cnt{font-size:11px;color:#888;margin-top:8px}
#lst{flex:1;overflow-y:auto;padding:6px}
.it{position:relative;padding:12px 14px;margin:3px 0;border-radius:10px;cursor:pointer;transition:background .15s;border:1px solid transparent}
.it:hover{background:#1a1a2e;border-color:#0f3460}
.it.on{background:#0f3460;border-color:#e94560}
.it .dt{font-size:10px;color:#888;margin-bottom:3px}
.it .pv{font-size:12px;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:26px}
.it .mt{font-size:10px;color:#666;margin-top:6px;display:flex;gap:10px;flex-wrap:wrap}
.it .del{position:absolute;top:8px;right:8px;width:24px;height:24px;border:none;border-radius:5px;background:transparent;color:#555;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s;z-index:2;line-height:1}
.it .del:hover{background:#e94560;color:#fff}
#mn{flex:1;display:flex;flex-direction:column;overflow:hidden}
#mn-hd{padding:16px 22px;border-bottom:1px solid #0f3460;background:#16213e}
#mn-hd h3{font-size:15px;color:#e0e0e0;word-break:break-word}
#mn-hd .sub{font-size:11px;color:#888;margin-top:4px}
#mn-hd .acts{display:flex;gap:10px;margin-top:14px}
#mn-hd .acts button{flex:1;padding:9px 0;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500;white-space:nowrap;transition:all .15s;text-align:center}
#mn-hd .ocur{border:none;background:#1a73e8;color:#fff}
#mn-hd .ocur:hover{background:#1565c0}
#mn-hd .ocur:disabled{opacity:.3;cursor:not-allowed}
#mn-hd .dcur{border:1px solid #555;background:transparent;color:#888}
#mn-hd .dcur:hover{border-color:#e94560;color:#e94560}
#mn-hd .dcur:disabled{opacity:.25;cursor:not-allowed}
#ca{flex:1;overflow-y:auto;padding:22px}
#em{display:flex;align-items:center;justify-content:center;height:100%;color:#555;font-size:15px}
.msg{margin-bottom:22px;display:flex;flex-direction:column}
.msg.u{align-items:flex-end}
.msg.a{align-items:flex-start}
.msg .bb{max-width:84%;padding:13px 18px;border-radius:14px;font-size:13px;line-height:1.7;word-break:break-word}
.msg.u .bb{background:#0f3460;color:#e0e0e0;border-bottom-right-radius:4px;white-space:pre-wrap}
.msg.a .bb{background:#1e2a4a;color:#e0e0e0;border-bottom-left-radius:4px}
.msg .mtm{font-size:9px;color:#666;margin:3px 8px}
.thb{margin-top:10px}
.thb summary{cursor:pointer;color:#888;font-size:11px;padding:5px 10px;background:#1a1a2e;border-radius:5px;display:inline-block;user-select:none}
.thb summary:hover{color:#e94560}
.thb .thc{margin-top:6px;padding:10px 14px;background:#111;color:#aaa;border-radius:6px;font-size:12px;line-height:1.6;white-space:pre-wrap;border-left:2px solid #333;max-height:360px;overflow-y:auto}
.bb pre{background:#0d1117;padding:12px;border-radius:7px;overflow-x:auto;margin:10px 0;font-size:12px;line-height:1.5}
.bb code{font-family:"SF Mono","Fira Code","Cascadia Code",monospace;font-size:.88em}
.bb p code,.bb li code{background:#0d1117;padding:2px 5px;border-radius:3px}
.bb p{margin:7px 0}
.bb ul,.bb ol{margin:7px 0;padding-left:22px}
.bb li{margin:3px 0}
.bb h1,.bb h2,.bb h3,.bb h4{margin:14px 0 7px 0;color:#e94560}
.bb blockquote{border-left:2px solid #e94560;padding-left:14px;margin:10px 0;color:#999}
.bb table{border-collapse:collapse;margin:10px 0;width:100%}
.bb td,.bb th{border:1px solid #333;padding:7px 10px;text-align:left}
.bb th{background:#0f3460}
.bb a{color:#e94560}
.bb hr{border:none;border-top:1px solid #333;margin:14px 0}
.mod{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.65);z-index:100;align-items:center;justify-content:center}
.mod.show{display:flex}
.mod .bx{background:#16213e;border:1px solid #0f3460;border-radius:12px;padding:26px;max-width:420px;width:90%}
.mod h3{color:#e94560;margin-bottom:10px}
.mod p{color:#aaa;font-size:13px;line-height:1.6;margin-bottom:18px}
.mod .pv2{color:#ccc;font-size:12px;background:#1a1a2e;padding:9px 12px;border-radius:7px;margin-bottom:18px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mod .bts{display:flex;gap:8px;justify-content:flex-end}
.mod .bts button{padding:7px 18px;border-radius:6px;border:none;cursor:pointer;font-size:12px}
.bcn{background:#333;color:#ccc}.bcn:hover{background:#444}
.bcf{background:#e94560;color:#fff}.bcf:hover{background:#ff5a6e}
#toast{position:fixed;bottom:22px;right:22px;padding:10px 18px;border-radius:7px;font-size:12px;z-index:200;background:#2e7d32;color:#fff;opacity:0;transform:translateY(8px);transition:all .3s;pointer-events:none;max-width:400px}
#toast.err{background:#c62828}
#toast.show{opacity:1;transform:translateY(0)}
</style>
</head>
<body>
<div id="sb">
  <div id="sb-hd">
    <h2>💬 聊天记录</h2>
    <div id="tdy"></div>
    <input id="q" type="text" placeholder="搜索..." />
    <div id="cnt"></div>
  </div>
  <div id="lst"></div>
</div>
<div id="mn">
  <div id="mn-hd">
    <div><h3 id="t1">选择会话</h3><div class="sub" id="t2"></div></div>
	    <div class="acts">
	      <button class="ocur" id="ocur" disabled onclick="openInClaude()">💻 在Claude中打开</button>
	      <button class="dcur" id="dcur" disabled onclick="delCurrent()">🗑 删除</button>
	    </div>
	  </div>
  <div id="ca"><div id="em">← 选择会话查看</div></div>
</div>
<div class="mod" id="mod">
  <div class="bx">
    <h3>确认删除</h3>
    <p>将永久删除该会话的所有记录：</p>
    <div class="pv2" id="mpv"></div>
    <div class="bts">
      <button class="bcn" onclick="closeMod()">取消</button>
      <button class="bcf" id="cfm">确认删除</button>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
const IS_SRV = IS_SERVER_MODE;
let D = [], aid = null, pid = null;

const lst = document.getElementById("lst"), ca = document.getElementById("ca"),
  q = document.getElementById("q"), cnt = document.getElementById("cnt"),
  t1 = document.getElementById("t1"), t2 = document.getElementById("t2"),
  dcur = document.getElementById("dcur"), ocur = document.getElementById("ocur"),
  toast = document.getElementById("toast");

if (typeof marked !== "undefined") marked.setOptions({breaks: true, gfm: true});

function fmt(n) { return n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : ""+n; }
function tsOk(m) { toast.textContent = m; toast.className = "show"; setTimeout(() => toast.classList.remove("show"), 2500); }
function tsErr(m) { toast.textContent = m; toast.className = "err show"; setTimeout(() => toast.classList.remove("show"), 2500); }

function openMod(sid) {
  pid = sid;
  const s = D.find(x => x.id === sid);
  document.getElementById("mpv").textContent = s ? s.preview : sid;
  document.getElementById("mod").classList.add("show");
  document.getElementById("cfm").onclick = doDel;
}
function closeMod() { document.getElementById("mod").classList.remove("show"); pid = null; }

function doDel() {
  if (!pid) return;
  const sid = pid; closeMod();
  if (IS_SRV) {
    fetch("/api/sessions/" + sid, { method: "DELETE" })
      .then(r => r.json())
      .then(d => {
        if (!d.success) throw new Error(d.message);
        D = D.filter(x => x.id !== sid);
        if (aid === sid) { aid = null; ca.innerHTML = '<div id="em">会话已删除</div>'; t1.textContent = "选择会话"; t2.textContent = ""; dcur.disabled = true; ocur.disabled = true; }
        renderList(q.value); updateToday(); tsOk("已删除");
      })
      .catch(e => tsErr("删除失败: " + e.message));
  } else {
    tsErr("静态模式不支持删除，请用服务模式");
  }
}

function delCurrent() { if (aid) openMod(aid); }
function openInClaude() {
  if (!aid) return;
  fetch("/api/sessions/" + aid + "/open", { method: "POST" })
    .then(r => r.json())
    .then(d => { if (d.success) tsOk(d.message); else tsErr("打开失败: " + d.message); })
    .catch(e => tsErr("请求失败: " + e.message));
}

function msgEl(m) {
  const d = document.createElement("div"); d.className = "msg " + (m.r === "user" ? "u" : "a");
  const b = document.createElement("div"); b.className = "bb";
  if (m.r === "assistant") {
    b.innerHTML = marked.parse(m.tx || "");
    if (m.th) {
      const det = document.createElement("details"); det.className = "thb";
      det.innerHTML = '<summary>💭 思考过程</summary><div class="thc"></div>';
      det.querySelector(".thc").textContent = m.th;
      b.appendChild(det);
    }
  } else { b.textContent = m.c; }
  d.appendChild(b);
  const mt = document.createElement("div"); mt.className = "mtm";
  let txt = m.t || "";
  if (m.m) txt += " · " + m.m;
  mt.textContent = txt; d.appendChild(mt);
  return d;
}

function renderSession(s) {
  ca.innerHTML = ""; t1.textContent = s.preview || "会话 " + s.id;
  t2.textContent = (s.date || "") + " · " + s.uc + " 条消息 · " + s.ac + " 条回复";
  dcur.disabled = false; ocur.disabled = !s.pex;
  if (!s.pex) ocur.title = "项目目录不存在，无法打开";
  else ocur.title = "";
  s.chat.forEach(m => ca.appendChild(msgEl(m))); ca.scrollTop = 0;
}

function renderList(filter) {
  const q2 = (filter || "").toLowerCase();
  const f = D.filter(s => !q2 || s.preview.toLowerCase().includes(q2) || s.id.toLowerCase().includes(q2));
  cnt.textContent = "共 " + f.length + " 个会话（总计 " + D.length + "）"; lst.innerHTML = "";
  f.forEach(s => {
    const div = document.createElement("div"); div.className = "it";
    if (s.id === aid) div.classList.add("on");
    var costStr = (s.cost != null) ? ('¥' + s.cost.toFixed(2)) : '-';
    var tokStr = fmt(s.tokens || 0);
    div.innerHTML = '<button class="del">✕</button><div class="dt">' + (s.date || "?") + '</div><div class="pv">' + (s.preview || "(空)") + '</div><div class="mt"><span>🧠 ' + (s.model || "?") + '</span><span>🔥 ' + tokStr + '</span><span>💰 ' + costStr + '</span></div>';
    div.querySelector(".del").addEventListener("click", e => { e.stopPropagation(); openMod(s.id); });
    div.addEventListener("click", () => { aid = s.id; renderSession(s); document.querySelectorAll(".it").forEach(el => el.classList.remove("on")); div.classList.add("on"); });
    lst.appendChild(div);
  });
}

function updateToday() {
  var today = new Date().toISOString().slice(0, 10);
  var todaySessions = D.filter(function(s) { return s.raw_date === today; });
  var todayTokens = 0, todayCost = 0;
  todaySessions.forEach(function(s) { todayTokens += (s.tokens || 0); todayCost += (s.cost || 0); });
  var el = document.getElementById("tdy");
  if (todaySessions.length > 0) {
    el.innerHTML = '📅 今日 <span>' + todaySessions.length + '</span> 会话 · <span>' + fmt(todayTokens) + '</span> token · <span>¥' + todayCost.toFixed(2) + '</span>';
  } else {
    el.innerHTML = '📅 今日暂无会话';
  }
}

function load() {
  if (IS_SRV) {
    fetch("/api/sessions").then(r => r.json()).then(data => { D = data; init(); }).catch(() => { lst.innerHTML = '<div style="padding:20px;color:#e94560;">⚠ 无法连接服务器</div>'; });
  } else {
    D = EMBEDDED_DATA; init();
  }
}
function init() {
  const hashId = window.location.hash.slice(1);
  if (hashId && D.find(x => x.id === hashId)) aid = hashId;
  else if (D.length > 0) aid = D[0].id;
  renderList(q.value);
  updateToday();
  if (aid) { const s = D.find(x => x.id === aid); if (s) renderSession(s); }
  else { dcur.disabled = true; ocur.disabled = true; }
}
q.addEventListener("input", () => renderList(q.value));
window.addEventListener("hashchange", () => {
  const h = window.location.hash.slice(1);
  if (h && h !== aid) { const s = D.find(x => x.id === h); if (s) { aid = h; renderSession(s); renderList(q.value); } }
});
// 心跳 + 页面关闭检测
setInterval(() => { fetch("/api/heartbeat"); }, 3000);
window.addEventListener("beforeunload", () => {
  navigator.sendBeacon("/api/shutdown");
});
load();
</script>
</body>
</html>'''


def generate_static(sessions, path):
    data = build_data(sessions)
    data_json = json.dumps(data, ensure_ascii=False)
    html = HTML.replace("IS_SERVER_MODE", "false").replace("EMBEDDED_DATA", data_json)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def serve_page():
    data = build_data(load_sessions())

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(b))
            self.end_headers()
            self.wfile.write(b)

        def _html(self, s, code=200):
            b = s.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(b))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = urlparse(self.path).path
            if p in ("/", "/index.html"):
                d = build_data(load_sessions())
                dj = json.dumps(d, ensure_ascii=False)
                html = HTML.replace("IS_SERVER_MODE", "true").replace("EMBEDDED_DATA", "[]")
                self._html(html)
            elif p == "/api/sessions":
                self._json(build_data(load_sessions()))
            elif p == "/api/heartbeat":
                self.server._last_hb = __import__("time").time()
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)

        def do_DELETE(self):
            p = urlparse(self.path).path
            prefix = "/api/sessions/"
            if p.startswith(prefix):
                sid = p[len(prefix):]
                if not sid:
                    self._json({"success": False, "message": "缺少 session ID"}, 400)
                    return
                ok, msg = delete_session(sid)
                self._json({"success": ok, "message": msg})
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/api/shutdown":
                self._json({"ok": True})
                import threading
                threading.Thread(target=self._shutdown, daemon=True).start()
            elif p.startswith("/api/sessions/") and p.endswith("/open"):
                sid = p[len("/api/sessions/"):-len("/open")]
                if not sid:
                    self._json({"success": False, "message": "缺少 session ID"}, 400)
                    return
                sessions = load_sessions()
                s = next((x for x in sessions if x["id"] == sid), None)
                if not s:
                    self._json({"success": False, "message": "会话不存在"}, 404)
                    return
                if not s["project_exists"]:
                    self._json({"success": False, "message": f"项目目录不存在: {s['project_path']}"}, 400)
                    return
                try:
                    _open_claude(s["project_path"], sid)
                    self._json({"success": True, "message": f"已在 {s['project_path']} 中打开"})
                except Exception as e:
                    self._json({"success": False, "message": f"启动失败: {e}"}, 500)

        def _shutdown(self):
            import time
            time.sleep(0.3)
            self.server.shutdown()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

    import time as _time, threading as _th
    srv = HTTPServer(("127.0.0.1", PORT), H)
    srv._last_hb = _time.time()
    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  🚀 {url}")
    print("  关闭浏览器即可自动退出\n")
    webbrowser.open(url)

    def _hb_check():
        while getattr(srv, '_alive', True):
            _time.sleep(5)
            if _time.time() - srv._last_hb > 10:
                srv.shutdown()
                break
    srv._alive = True
    _th.Thread(target=_hb_check, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv._alive = False
    srv.server_close()
    print("  已停止")


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == "--export":
            sessions = load_sessions()
            out = Path.home() / "Desktop" / "claude-chats.html"
            generate_static(sessions, out)
            print(f"✅ {out}")
            return
        elif sys.argv[1] == "--delete" and len(sys.argv) > 2:
            ok, msg = delete_session(sys.argv[2])
            print(f"{'✅' if ok else '❌'} {msg}")
            return
        else:
            print("用法: python chat-viewer.py [--export|--delete <id>]")
            return

    sessions = load_sessions()
    total_tk = sum(s["tokens"] for s in sessions)
    print(f"📊 {len(sessions)} 个会话 | {total_tk:,} tokens")
    serve_page()


if __name__ == "__main__":
    main()
