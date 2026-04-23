"""Gera scripts/class_weekdays.json com weekday REAL por class_id.

Difere de discover_weekdays.py (que apenas cruza modalidade+hora com a
grade textual — logo não distingue IDs que compartilham modalidade+hora
mas rodam em dias diferentes, ex: Seven Bike 06:00 Ter vs Qui).

Este bate na CloudGym (`/admin/classattendancelist/{unit}/{date}/{class_id}`)
para cada combinação (class_id, weekday candidato) e usa a presença/ausência
de items como oráculo.

Execução SERIAL com delay entre requests para evitar rate-limit 429.
Se uma request falhar (timeout/429), PRESERVA o valor antigo daquele ID
em vez de sobrescrever com lista vazia.

Uso:
    cd clientes/seven
    python -m scripts.discover_weekdays_precise
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import cloudgym  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("discover_weekdays_precise")

CATALOG_PATH = ROOT / "scripts" / "class_catalog.json"
OUTPUT_PATH = ROOT / "scripts" / "class_weekdays.json"

_WD_MAP = {
    "Segunda": 0, "Terça": 1, "Terca": 1, "Quarta": 2, "Quinta": 3,
    "Sexta": 4, "Sábado": 5, "Sabado": 5, "Domingo": 6,
}
_WD_NAMES = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

# Testa 2 ocorrências futuras do weekday — robusto contra cancelamento pontual.
N_SAMPLES = 2

# Delay entre requests (segundos) — evita 429 da CloudGym.
REQUEST_DELAY = 1.5

# Se SKIP_FILLED=True, pula IDs que já têm weekdays no JSON atual.
SKIP_FILLED = True

# Marker sentinel para erro irrecuperável (manter valor antigo no JSON).
ERROR = object()


def next_dates_for_weekday(wd: int, n: int = N_SAMPLES, base: date | None = None) -> list[str]:
    base = base or date.today()
    days_ahead = (wd - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return [(base + timedelta(days=days_ahead + 7 * i)).isoformat() for i in range(n)]


async def _raw_get_availability(date_str: str, class_id: str) -> httpx.Response:
    """Versão sem retry interno para controlarmos backoff manualmente."""
    from app.config import settings
    token = await cloudgym._get_v1_token()
    url = f"{settings.CLOUDGYM_V1_BASE}/admin/classattendancelist/{settings.CLOUDGYM_UNIT_ID}/{date_str}/{class_id}"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    client = cloudgym._get_client()
    return await client.get(url, headers=headers)


async def class_runs_on(class_id: str, date_str: str) -> bool | object:
    """True se retorna items populados; False se 200+vazio; ERROR se falhou."""
    for attempt in range(4):
        try:
            resp = await _raw_get_availability(date_str, class_id)
        except Exception as e:
            log.warning("class_id=%s date=%s erro de rede %s — sleep 5s", class_id, date_str, e)
            await asyncio.sleep(5)
            continue

        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            log.warning("class_id=%s date=%s HTTP 429 (tentativa %d/4) — sleep %ds",
                        class_id, date_str, attempt + 1, wait)
            await asyncio.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = 3 * (attempt + 1)
            log.warning("class_id=%s date=%s HTTP %d — sleep %ds",
                        class_id, date_str, resp.status_code, wait)
            await asyncio.sleep(wait)
            continue

        if resp.status_code == 404:
            return False

        if not (200 <= resp.status_code < 300):
            log.warning("class_id=%s date=%s HTTP %d — tratando como não-roda",
                        class_id, date_str, resp.status_code)
            return False

        try:
            data = resp.json()
        except Exception:
            return False
        items = data if isinstance(data, list) else (data.get("items") or data.get("content") or [])
        return bool(items)

    return ERROR


async def discover_for_class(class_id: str, candidates: list[int]) -> tuple[list[int], bool]:
    """Retorna (weekdays_reais, tudo_ok). tudo_ok=False se algum teste deu ERROR."""
    actual: list[int] = []
    had_error = False
    for wd in candidates:
        hit = False
        for d in next_dates_for_weekday(wd):
            r = await class_runs_on(class_id, d)
            await asyncio.sleep(REQUEST_DELAY)
            if r is ERROR:
                had_error = True
                continue
            if r is True:
                hit = True
                break
        if hit:
            actual.append(wd)
    return sorted(actual), not had_error


async def main() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    log.info("Carregado catálogo com %d class_ids", len(catalog))

    existing: dict[str, list[int]] = {}
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    out: dict[str, list[int]] = dict(existing)

    for cid, meta in catalog.items():
        grade_names = meta.get("weekdays_grade", [])
        candidates = sorted({_WD_MAP[n] for n in grade_names if n in _WD_MAP})
        if not candidates:
            log.warning("class_id=%s sem weekdays_grade — pulando", cid)
            continue

        if SKIP_FILLED and existing.get(cid):
            log.info("%s  %s  %s  já preenchido: %s — pulando",
                     cid, meta.get("modalidade"), meta.get("hora"),
                     [_WD_NAMES[w] for w in existing[cid]])
            continue

        actual, all_ok = await discover_for_class(cid, candidates)

        if all_ok:
            out[cid] = actual
            tag = "OK"
        else:
            prev = existing.get(cid, [])
            if actual:
                merged = sorted(set(actual))
                out[cid] = merged
                tag = f"PARCIAL (mantendo {merged}, prev={prev})"
            else:
                out[cid] = prev
                tag = f"ERRO — preservando valor antigo {prev}"

        log.info(
            "%s  %s  %s  cand=%s real=%s [%s]",
            cid, meta.get("modalidade"), meta.get("hora"),
            [_WD_NAMES[w] for w in candidates],
            [_WD_NAMES[w] for w in out[cid]] if out[cid] else "VAZIO",
            tag,
        )

        # Persiste a cada ID — se o script travar no meio, não perdemos tudo.
        OUTPUT_PATH.write_text(
            json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    log.info("Salvo em %s (%d IDs)", OUTPUT_PATH, len(out))


if __name__ == "__main__":
    asyncio.run(main())
