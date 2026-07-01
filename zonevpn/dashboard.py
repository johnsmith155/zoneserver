"""Tiny local web dashboard for the ZoneVPN server.

Run it:   ./venv/bin/python -m zonevpn.dashboard
          (install.sh installs it as the `zonevpn-dashboard` systemd service)

What it shows / does:
  • Overall status   — last cycle time, published count, signed/base64 flags.
  • Live logs        — tails state/zonevpn.log.
  • Server table     — the *decoded* published list (name, ping, country, flag,
                       protocol, host:port) even though the gist is base64.
  • Delete           — drops a server: adds it to the blocklist (so it never
                       comes back) AND immediately re-publishes the gist without
                       it, so it's gone from the app right away.
  • Update           — runs update.sh (hard `git reset` + restart) via sudo.

Security: set `dashboard_token` in config.json. Every request must then carry it
(?token=... on the page URL; the JS forwards it as a header for API calls). If
the token is empty, the dashboard is unauthenticated — only do that behind a
firewall / SSH tunnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from . import config as cfgmod
from . import gist, links, sign, state

log = logging.getLogger("zonevpn.dashboard")

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = ROOT / "update.sh"
SERVICE_NAME = "zonevpn"


# --------------------------------------------------------------------------- #
# helpers                                                                       #
# --------------------------------------------------------------------------- #
def _service_active() -> str:
    try:
        out = subprocess.run(["systemctl", "is-active", SERVICE_NAME],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:])
    except OSError:
        return "(no logs yet)"


def _republish_without(deleted_key: str) -> tuple[bool, str]:
    """Rebuild the payload from the local snapshot minus the deleted server and
    push it to the gist now, so the app stops seeing it immediately."""
    cfg, cfg_err = cfgmod.load_lenient()
    if cfg_err:
        return False, f"config.json is broken: {cfg_err}"
    if not cfg.get("github_token") or not cfg.get("gist_id"):
        return False, "github_token/gist_id not configured"

    servers = [s for s in state.read_servers()
               if s.get("block_key") != deleted_key]
    # Strip dashboard-only fields so the public gist keeps its clean shape.
    _internal = {"block_key", "exit_ip", "front"}
    configs = [{k: v for k, v in s.items() if k not in _internal} for s in servers]
    payload = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(configs),
        "configs": configs,
        "raw": "\n".join(c.get("config", "") for c in configs),
    }
    sign_key = sign.load_private_key(cfg)
    ok = gist.publish(
        cfg["github_token"], cfg["gist_id"], cfg["gist_filename"], payload,
        base64_encode=bool(cfg.get("gist_base64", True)),
        sign_key_b64=sign_key,
    )
    if ok:
        # reflect the deletion in the local snapshot too
        state.write_servers(servers)
    return ok, ("re-published" if ok else "gist publish failed")


# --------------------------------------------------------------------------- #
# auth middleware                                                               #
# --------------------------------------------------------------------------- #
@web.middleware
async def _auth(request: web.Request, handler):
    token = request.app["token"]
    if token:
        supplied = (request.query.get("token")
                    or request.headers.get("X-Dashboard-Token")
                    or "")
        if supplied != token:
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


# --------------------------------------------------------------------------- #
# routes                                                                        #
# --------------------------------------------------------------------------- #
async def index(_request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def api_status(request: web.Request) -> web.Response:
    return web.json_response({
        "status": state.read_status(),
        "servers": state.read_servers(),
        "blocklist": state.load_blocklist(),
        "manual": state.read_manual(),
        "progress": state.read_progress(),
        "service": _service_active(),
        "config_error": request.app.get("config_error"),
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


async def api_add(request: web.Request) -> web.Response:
    """Add a server by share link. It's parsed/validated, stored, and picked up
    (tested + published if it's actually fast) on the next cycle."""
    body = await request.json()
    link = (body.get("link") or "").strip()
    if not link:
        return web.json_response({"error": "link required"}, status=400)
    parsed = links.parse_link(link)
    if parsed is None:
        return web.json_response(
            {"ok": False,
             "message": "unrecognized link (need vmess/vless/trojan/ss://)"},
            status=400)
    manual = state.add_manual(link)
    return web.json_response({
        "ok": True,
        "message": f"added {parsed.protocol} {parsed.address}:{parsed.port} — "
                   f"it will be tested on the next cycle",
        "count": len(manual),
    })


async def api_remove_manual(request: web.Request) -> web.Response:
    body = await request.json()
    link = (body.get("link") or "").strip()
    if not link:
        return web.json_response({"error": "link required"}, status=400)
    state.remove_manual(link)
    return web.json_response({"ok": True})


async def api_logs(request: web.Request) -> web.Response:
    n = int(request.query.get("n", "300") or "300")
    return web.json_response({"log": _tail(state.LOG_FILE, max(1, min(n, 2000)))})


async def api_delete(request: web.Request) -> web.Response:
    body = await request.json()
    key = (body.get("block_key") or "").strip()
    if not key:
        return web.json_response({"error": "block_key required"}, status=400)
    state.add_to_blocklist(key)
    ok, msg = _republish_without(key)
    return web.json_response({"ok": ok, "message": msg, "block_key": key})


async def api_restore(request: web.Request) -> web.Response:
    body = await request.json()
    key = (body.get("block_key") or "").strip()
    if not key:
        return web.json_response({"error": "block_key required"}, status=400)
    state.remove_from_blocklist(key)
    return web.json_response({"ok": True, "block_key": key})


async def api_update(_request: web.Request) -> web.Response:
    if not UPDATE_SCRIPT.exists():
        return web.json_response({"ok": False, "message": "update.sh missing"},
                                 status=500)
    try:
        # Detached so it survives this (dashboard) service being restarted.
        subprocess.Popen(["sudo", "-n", str(UPDATE_SCRIPT)],
                         cwd=str(ROOT), start_new_session=True)
        return web.json_response({"ok": True,
                                  "message": "update started; services restarting…"})
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=500)


def build_app(token: str, config_error: str | None = None) -> web.Application:
    app = web.Application(middlewares=[_auth])
    app["token"] = token
    app["config_error"] = config_error
    app.add_routes([
        web.get("/", index),
        web.get("/api/status", api_status),
        web.get("/api/logs", api_logs),
        web.post("/api/delete", api_delete),
        web.post("/api/restore", api_restore),
        web.post("/api/add", api_add),
        web.post("/api/remove_manual", api_remove_manual),
        web.post("/api/update", api_update),
    ])
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Lenient load: even if config.json is broken, still bring the console up so
    # the operator can see the error + logs and fix it (best-effort token).
    cfg, cfg_err = cfgmod.load_lenient()
    host = cfg.get("dashboard_host", "0.0.0.0")
    port = int(cfg.get("dashboard_port", 8787))
    token = str(cfg.get("dashboard_token", "") or "")
    state.ensure_dir()
    if cfg_err:
        log.error("config.json problem: %s — running in LIMITED mode "
                  "(publish/delete disabled until fixed)", cfg_err)
    if not token:
        log.warning("dashboard_token is empty — the dashboard is UNAUTHENTICATED. "
                    "Set dashboard_token in config.json or firewall the port.")
    log.info("ZoneVPN dashboard on http://%s:%d  (token %s)",
             host, port, "required" if token else "DISABLED")
    web.run_app(build_app(token, cfg_err), host=host, port=port, print=None)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ZoneVPN · Server</title>
<style>
  :root{
    --bg:#0b0f17; --panel:#121826; --panel2:#0e1420; --stroke:#1e2638;
    --txt:#e6ecf5; --txt2:#9aa7bd; --txt3:#5f6c83;
    --accent:#6d5efc; --accent2:#27d4a8; --danger:#ff5d6c; --warn:#ffb020;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,#1a2440 0,var(--bg) 55%);
    color:var(--txt);font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    min-height:100vh}
  .wrap{max-width:1080px;margin:0 auto;padding:22px 18px 60px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:18px}
  .logo{font-weight:800;font-size:20px;letter-spacing:.3px;
    background:linear-gradient(90deg,#8b7bff,#27d4a8);-webkit-background-clip:text;
    background-clip:text;color:transparent}
  .pill{margin-left:auto;display:flex;gap:8px;align-items:center}
  .dot{width:9px;height:9px;border-radius:50%}
  .btn{border:1px solid var(--stroke);background:var(--panel);color:var(--txt);
    padding:8px 14px;border-radius:10px;cursor:pointer;font-weight:600;font-size:13px}
  .btn:hover{border-color:#2b3650}
  .btn.primary{background:linear-gradient(90deg,#6d5efc,#5a8bff);border:0;color:#fff}
  .btn.danger{color:var(--danger);border-color:#3a2330}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--stroke);border-radius:14px;padding:14px}
  .card .k{color:var(--txt3);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:22px;font-weight:700;margin-top:6px}
  .grid{display:grid;grid-template-columns:1fr;gap:16px}
  .panel{background:var(--panel);border:1px solid var(--stroke);border-radius:16px;overflow:hidden}
  .panel h2{margin:0;padding:14px 16px;font-size:14px;border-bottom:1px solid var(--stroke);
    display:flex;align-items:center;gap:10px}
  .panel h2 .sub{color:var(--txt3);font-weight:500;font-size:12px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--panel2);font-size:13px}
  th{color:var(--txt3);text-transform:uppercase;font-size:11px;letter-spacing:.5px;position:sticky;top:0;background:var(--panel)}
  tr:hover td{background:#0f1626}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--txt2)}
  .ping{font-weight:700}
  .ping.good{color:var(--accent2)} .ping.mid{color:var(--warn)} .ping.bad{color:var(--danger)}
  .tablewrap{max-height:430px;overflow:auto}
  pre#log{margin:0;padding:14px 16px;max-height:360px;overflow:auto;white-space:pre-wrap;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#bcd0ea;background:var(--panel2)}
  .row{display:flex;gap:10px;align-items:center;padding:12px 16px;border-top:1px solid var(--stroke)}
  .muted{color:var(--txt3)} .right{margin-left:auto}
  .banner{background:#3a1620;border:1px solid #5e2330;color:#ffb4bd;padding:12px 16px;
    border-radius:12px;margin-bottom:16px;font-weight:600;font-size:13px}
  .v2{font-size:18px;font-weight:700;margin-top:4px}
  .bar{height:5px;background:#1e2638;border-radius:3px;overflow:hidden;margin:0 16px 12px}
  .bar > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#6d5efc,#27d4a8);transition:width .4s}
  .addrow{display:flex;gap:10px;padding:12px 16px;border-top:1px solid var(--stroke)}
  .addrow input{flex:1;background:var(--panel2);border:1px solid var(--stroke);color:var(--txt);
    padding:9px 12px;border-radius:10px;font-size:13px;font-family:ui-monospace,monospace}
  .manhdr{padding:6px 16px 2px;color:var(--txt3);font-size:12px}
  .manrow{display:flex;gap:10px;align-items:center;padding:6px 16px;border-top:1px solid var(--panel2)}
  .manrow span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
  #feed{margin:0;padding:12px 16px;max-height:170px;overflow:auto;white-space:pre-wrap;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#bcd0ea;background:var(--panel2)}
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#121a2c;
    border:1px solid var(--stroke);padding:12px 18px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1}
  @media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="logo">ZoneVPN</span><span class="muted">server console</span>
    <span class="pill"><span id="svcDot" class="dot" style="background:#5f6c83"></span>
      <span id="svc" class="muted">…</span>
      <button class="btn" onclick="refresh()">↻ Refresh</button>
      <button class="btn primary" id="updBtn" onclick="doUpdate()">⬆ Update server</button>
    </span>
  </header>

  <div id="cfgErr" class="banner" style="display:none"></div>

  <div class="panel" style="margin-bottom:18px">
    <h2>Current cycle <span class="sub" id="cyPhase">…</span>
      <span class="right muted" id="cyThreads"></span></h2>
    <div class="row" style="border-top:0;flex-wrap:wrap;gap:26px">
      <div><div class="k">Collected</div><div class="v2" id="cyCollected">–</div></div>
      <div><div class="k">Reachable (TCP)</div><div class="v2" id="cyReachable">–</div></div>
      <div><div class="k">Tested</div><div class="v2" id="cyTested">–</div></div>
      <div><div class="k">Alive</div><div class="v2" id="cyAlive">–</div></div>
    </div>
    <div class="bar"><i id="cyBar"></i></div>
    <pre id="feed">(no live results yet)</pre>
  </div>

  <div class="cards">
    <div class="card"><div class="k">Published</div><div class="v" id="cCount">–</div></div>
    <div class="card"><div class="k">Last cycle</div><div class="v" id="cUpdated">–</div></div>
    <div class="card"><div class="k">Cycle time</div><div class="v" id="cDur">–</div></div>
    <div class="card"><div class="k">Protection</div><div class="v" id="cSec">–</div></div>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>Servers <span class="sub" id="sCount"></span>
        <button class="btn right" onclick="refresh()">Reload</button></h2>
      <div class="addrow">
        <input id="addLink" placeholder="Paste vmess:// vless:// trojan:// ss:// link to add a server"/>
        <button class="btn primary" onclick="addServer()">+ Add</button>
      </div>
      <div id="manualList"></div>
      <div class="tablewrap">
        <table>
          <thead><tr><th>#</th><th>Server</th><th>Front (host:port)</th><th>Exit IP</th><th>Country</th><th>Ping</th><th>TCP</th><th>Proto</th><th></th></tr></thead>
          <tbody id="rows"><tr><td colspan="9" class="muted">loading…</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h2>Live logs <span class="sub">state/zonevpn.log (auto-refresh)</span>
        <label class="right muted" style="font-weight:500"><input type="checkbox" id="autolog" checked> auto</label></h2>
      <pre id="log">loading…</pre>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const H = TOKEN ? {'X-Dashboard-Token': TOKEN} : {};
const $ = id => document.getElementById(id);
let MANUAL = [];
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2600);}

function pingClass(p){if(p<0)return 'bad';if(p<=300)return 'good';if(p<=900)return 'mid';return 'bad';}
function ago(iso){if(!iso)return '–';const s=(Date.now()-new Date(iso))/1000;
  if(s<60)return Math.round(s)+'s ago';if(s<3600)return Math.round(s/60)+'m ago';
  return Math.round(s/3600)+'h ago';}

async function refresh(){
  try{
    const r = await fetch('/api/status',{headers:H});
    if(r.status===401){$('rows').innerHTML='<tr><td colspan=7 class=muted>Unauthorized — append ?token=YOUR_TOKEN to the URL.</td></tr>';return;}
    const d = await r.json();
    const st = d.status||{};
    $('cCount').textContent = st.count ?? (d.servers?d.servers.length:'–');
    $('cUpdated').textContent = ago(st.updated_at);
    $('cDur').textContent = st.duration_s!=null ? st.duration_s+'s' : '–';
    $('cSec').textContent = st.signed ? 'Signed' : (st.base64 ? 'Base64' : 'Plain');
    const active = d.service==='active';
    $('svc').textContent = 'service: '+d.service;
    $('svcDot').style.background = active?'#27d4a8':'#ff5d6c';
    const rows=(d.servers||[]).map((s,i)=>{
      const pc=pingClass(s.ping);
      return `<tr>
        <td class=muted>${i+1}</td>
        <td>${s.name||''}</td>
        <td class=mono>${s.front||s.block_key||''}</td>
        <td class=mono>${s.exit_ip||'—'}</td>
        <td>${s.flag||''} ${s.country||'??'}</td>
        <td class="ping ${pc}">${s.ping<0?'—':s.ping+' ms'}</td>
        <td class=muted>${s.tcp_ping==null||s.tcp_ping<0?'—':s.tcp_ping+' ms'}</td>
        <td class=muted>${s.protocol||''}</td>
        <td><button class="btn danger" onclick="del('${s.block_key}')">Delete</button></td>
      </tr>`;}).join('');
    $('rows').innerHTML = rows || '<tr><td colspan=9 class=muted>No servers yet — wait for the next cycle.</td></tr>';
    $('sCount').textContent = (d.servers?d.servers.length:0)+' published'
      + (d.manual&&d.manual.length?(' · '+d.manual.length+' manual'):'')
      + (d.blocklist&&d.blocklist.length?(' · '+d.blocklist.length+' blocked'):'');

    // manual servers list (operator-added; always kept and tested each cycle)
    MANUAL = d.manual || [];
    $('manualList').innerHTML = MANUAL.length
      ? '<div class=manhdr>Manual servers (added by you — kept every cycle):</div>'
        + MANUAL.map((m,i)=>`<div class=manrow><span class=mono title="${esc(m)}">${esc(m)}</span>`
          + `<button class="btn danger" onclick="delManual(${i})">remove</button></div>`).join('')
      : '';

    // config error banner
    const ce=$('cfgErr');
    if(d.config_error){ce.style.display='block';
      ce.textContent='⚠ config.json problem: '+d.config_error+
        '  — fix it, then: systemctl restart zonevpn zonevpn-dashboard';}
    else{ce.style.display='none';}

    // current cycle (live)
    const pr=d.progress||{}, th=pr.threads||{};
    $('cyPhase').textContent=(pr.active?'▶ ':'')+(pr.phase||'idle');
    $('cyThreads').textContent= th.measure_concurrency!=null
      ? ('threads: '+th.measure_concurrency+' probes · '+th.parallel_batches+' batches × '+th.batch_size)
      : '';
    $('cyCollected').textContent = pr.collected ?? '–';
    $('cyReachable').textContent = pr.reachable ?? '–';
    $('cyTested').textContent = (pr.tested!=null?pr.tested:'–')+(pr.total?(' / '+pr.total):'');
    $('cyAlive').textContent = pr.alive ?? '–';
    const pct = pr.total? Math.min(100,Math.round((pr.tested||0)/pr.total*100)) : 0;
    $('cyBar').style.width = pct+'%';
    const feed=(pr.recent||[]).slice().reverse()
      .map(r=>`${String(r.ping).padStart(4)} ms   ${r.address}:${r.port}  (${r.protocol})`).join('\n');
    $('feed').textContent = feed || '(no live results yet)';
  }catch(e){toast('status failed');}
}

async function addServer(){
  const el=$('addLink'); const link=(el.value||'').trim();
  if(!link){toast('paste a vmess/vless/trojan/ss link first');return;}
  try{const r=await fetch('/api/add',{method:'POST',
      headers:{'Content-Type':'application/json',...H},body:JSON.stringify({link})});
    const d=await r.json();
    toast(d.ok?('Added · '+d.message):('Add failed · '+(d.message||d.error)));
    if(d.ok){el.value='';refresh();}}
  catch(e){toast('add failed');}
}

async function delManual(i){
  const link=MANUAL[i]; if(!link) return;
  try{const r=await fetch('/api/remove_manual',{method:'POST',
      headers:{'Content-Type':'application/json',...H},body:JSON.stringify({link})});
    await r.json(); toast('Removed manual server'); refresh();}
  catch(e){toast('remove failed');}
}

async function loadLog(){
  if(!$('autolog').checked) return;
  try{const r=await fetch('/api/logs?n=1500',{headers:H});const d=await r.json();
    const el=$('log');const stick=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
    el.textContent=d.log||'';if(stick)el.scrollTop=el.scrollHeight;}catch(e){}
}

async function del(key){
  if(!confirm('Delete this server?\n'+key+'\nIt will be blocked and removed from the app now.'))return;
  try{const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json',...H},
    body:JSON.stringify({block_key:key})});const d=await r.json();
    toast(d.ok?('Deleted · '+d.message):('Delete failed · '+(d.message||d.error)));refresh();}
  catch(e){toast('delete failed');}
}

async function doUpdate(){
  if(!confirm('Hard-update the server?\nThis stops the service, pulls the latest code, reinstalls deps and restarts.'))return;
  $('updBtn').disabled=true;
  try{const r=await fetch('/api/update',{method:'POST',headers:H});const d=await r.json();
    toast(d.ok?'Update started — services restarting…':('Update failed · '+(d.message||d.error)));}
  catch(e){toast('Update triggered (connection dropped as expected).');}
  setTimeout(()=>{$('updBtn').disabled=false;refresh();},8000);
}

refresh(); loadLog();
setInterval(refresh,5000);
setInterval(loadLog,4000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
