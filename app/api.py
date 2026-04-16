"""
Rotas de observabilidade/logs. Prefixo: /{CLIENT_SLUG}/
"""
import json
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import settings
from app.services.redis_service import get_redis, get_chat_history
from app.db import list_all_leads

logger = logging.getLogger(__name__)
router = APIRouter(prefix=f"/{settings.CLIENT_SLUG}")

_KEY_SUFFIX = f"--{settings.CLIENT_SLUG}"


@router.get("/logs/leads")
async def logs_leads():
    leads = await list_all_leads()
    result = []
    for lead in leads:
        result.append({
            "phone": lead.get("phone", ""),
            "nome": lead.get("nome"),
            "nicho": lead.get("status"),
            "msg_count": 0,
            "resumo": None,
        })
    return result


@router.get("/logs/history/{phone}")
async def logs_history(phone: str):
    history = await get_chat_history(phone)
    result = []
    for entry in history:
        if "role" in entry and "parts" in entry:
            role = "ai" if entry["role"] == "model" else "human"
            text = entry.get("parts", [{}])[0].get("text", "")
            result.append({"role": role, "content": text})
    return result


@router.get("/logs/events")
async def logs_events(limit: int = 100):
    r = await get_redis()
    raw = await r.lrange(f"{settings.CLIENT_SLUG}:logs", 0, limit - 1)
    events = []
    for item in raw:
        try:
            events.append(json.loads(item))
        except Exception:
            pass
    return events


@router.get("/painel", response_class=HTMLResponse)
async def painel():
    slug = settings.CLIENT_SLUG
    name = settings.CLIENT_NAME
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{name} — Painel</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #111; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }}
  h1 {{ color: #27ae60; font-size: 18px; margin-bottom: 4px; }}
  #status {{ font-size: 11px; color: #666; margin-bottom: 16px; }}
  .event {{ border: 1px solid #2a2a2a; border-radius: 6px; padding: 10px 14px; margin-bottom: 10px; background: #1a1a1a; }}
  .event-header {{ color: #555; font-size: 11px; margin-bottom: 8px; border-bottom: 1px solid #2a2a2a; padding-bottom: 5px; }}
  .event-header .phone {{ color: #3498db; font-weight: bold; }}
  .log-line {{ margin: 3px 0; font-size: 12px; line-height: 1.5; }}
  .new-badge {{ display: inline-block; background: #27ae60; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 8px; }}
</style>
</head>
<body>
<h1>{name} — Execucoes</h1>
<div id="status">Carregando...</div>
<div id="events"></div>
<script>
let lastTs = null;

function fmt(ts) {{
  return new Date(ts * 1000).toLocaleString('pt-BR', {{ timeZone: 'America/Sao_Paulo' }});
}}

async function refresh() {{
  try {{
    const res = await fetch('/{slug}/logs/events?limit=50');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const events = await res.json();
    const container = document.getElementById('events');
    const status = document.getElementById('status');

    if (!events.length) {{
      status.textContent = 'Nenhuma execucao registrada ainda.';
      return;
    }}

    const newest = events[0].ts;
    const isNew = newest !== lastTs;

    if (isNew) {{
      container.innerHTML = '';
      for (let i = 0; i < events.length; i++) {{
        const ev = events[i];
        const div = document.createElement('div');
        div.className = 'event';

        const header = document.createElement('div');
        header.className = 'event-header';
        header.innerHTML = fmt(ev.ts) + ' &nbsp;-&nbsp; <span class="phone">' + (ev.phone || '') + '</span>'
          + (i === 0 && lastTs !== null ? '<span class="new-badge">NOVO</span>' : '');
        div.appendChild(header);

        for (const line of (ev.lines || [])) {{
          const p = document.createElement('p');
          p.className = 'log-line';
          p.innerHTML = line;
          div.appendChild(p);
        }}
        container.appendChild(div);
      }}
      lastTs = newest;
    }}

    const now = new Date().toLocaleTimeString('pt-BR');
    status.textContent = 'Atualizado: ' + now + ' - ' + events.length + ' execucao(oes)';
  }} catch (e) {{
    document.getElementById('status').textContent = 'Erro: ' + e.message;
  }}
}}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
    return html
