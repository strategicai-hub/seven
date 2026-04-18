"""
Cliente HTTP para a API CloudGym. Unit ID 2751.

Usa duas autenticações diferentes:
- v1 (api.prod.cloudgym.io): Basic Auth -> retorna access_token (Bearer). Cache em Redis.
- v2 (api.cloudgym.io): POST /auth com username/password -> retorna token. Cache em Redis.

Endpoints v1: /config/classes/{unit}, /admin/classattendancelist, /v1/classattendance, /customer
Endpoints v2: /v1/member (listagem/busca de alunos)
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx

from app.config import settings
from app.data.class_catalog import TRIAL_PLAN_EXT_ID
from app.services import redis_service as rds

logger = logging.getLogger(__name__)

_V1_TOKEN_KEY = "cg:v1:token"
_V2_TOKEN_KEY = "cg:v2:token"

_client: httpx.AsyncClient | None = None

# Fallback in-process quando Redis está indisponível. Mantém tokens em memória
# até o expiry, evitando bater em /auth/token a cada chamada.
_mem_token_cache: dict[str, tuple[str, float]] = {}

# Locks para single-flight no fetch de token. Sem isso, quando várias corotinas
# entram em paralelo com cache vazio, todas disparam /auth/token ao mesmo tempo
# e a CloudGym pode responder 429.
_v1_token_lock = asyncio.Lock()
_v2_token_lock = asyncio.Lock()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"timeout": 30}
        if settings.CLOUDGYM_PROXY:
            kwargs["proxy"] = settings.CLOUDGYM_PROXY
        _client = httpx.AsyncClient(**kwargs)
    return _client


async def _cache_get(key: str) -> str | None:
    import time
    cached = _mem_token_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        val = await rds.cache_get(key)
        return val
    except Exception as e:
        logger.debug("Redis cache_get falhou (%s=%s) — seguindo com memória", key, e)
        return None


async def _cache_set(key: str, value: str, ttl: int) -> None:
    import time
    _mem_token_cache[key] = (value, time.time() + ttl)
    try:
        await rds.cache_set(key, value, ttl)
    except Exception as e:
        logger.debug("Redis cache_set falhou (%s) — só memória: %s", key, e)


async def _get_v1_token() -> str:
    cached = await _cache_get(_V1_TOKEN_KEY)
    if cached:
        return cached

    async with _v1_token_lock:
        cached = await _cache_get(_V1_TOKEN_KEY)
        if cached:
            return cached

        url = f"{settings.CLOUDGYM_V1_BASE}/auth/token"
        headers = {
            "accept": "*/*",
            "Authorization": f"Basic {settings.CLOUDGYM_V1_BASIC}",
        }
        client = _get_client()
        # CloudGym v1 expõe /auth/token como GET (resposta 405 com Allow: GET para POST).
        resp = await _request_with_retry(client, "GET", url, headers=headers)
        data = resp.json()
        token = data.get("access_token") or data.get("token") or ""
        expires_in = int(data.get("expires_in") or 600)
        if token:
            await _cache_set(_V1_TOKEN_KEY, token, ttl=max(60, expires_in - 30))
        return token


async def _get_v2_token() -> str:
    cached = await _cache_get(_V2_TOKEN_KEY)
    if cached:
        return cached

    async with _v2_token_lock:
        cached = await _cache_get(_V2_TOKEN_KEY)
        if cached:
            return cached

        # CloudGym v2: POST /auth com Basic Auth (username/password como credenciais HTTP Basic),
        # corpo vazio. Resposta: {"success": true, "accessToken": "..."}.
        url = f"{settings.CLOUDGYM_V2_BASE}/auth"
        client = _get_client()
        resp = await _request_with_retry(
            client, "POST", url, auth=(settings.CLOUDGYM_V2_USERNAME, settings.CLOUDGYM_V2_PASSWORD)
        )
        data = resp.json()
        token = data.get("accessToken") or data.get("token") or data.get("access_token") or ""
        expires_in = int(data.get("expires_in") or 3600)
        if token:
            await _cache_set(_V2_TOKEN_KEY, token, ttl=max(60, expires_in - 30))
        return token


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json: Any = None,
    auth: Any = None,
    max_retries: int = 3,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(method, url, headers=headers, params=params, json=json, auth=auth)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Server error {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            wait = 2 * (attempt + 1)
            logger.warning("CloudGym %s %s: tentativa %d/%d falhou (%s). Aguardando %ds",
                           method, url, attempt + 1, max_retries, e, wait)
            await asyncio.sleep(wait)
    raise last_exc  # type: ignore[misc]


async def _v1_get(path: str, params: Optional[dict] = None) -> dict:
    token = await _get_v1_token()
    url = f"{settings.CLOUDGYM_V1_BASE}{path}"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    resp = await _request_with_retry(_get_client(), "GET", url, headers=headers, params=params)
    return resp.json()


async def _v1_post(path: str, json: Any) -> dict:
    token = await _get_v1_token()
    url = f"{settings.CLOUDGYM_V1_BASE}{path}"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    resp = await _request_with_retry(_get_client(), "POST", url, headers=headers, json=json)
    return resp.json()


async def _v2_get(path: str, params: Optional[dict] = None) -> dict:
    token = await _get_v2_token()
    url = f"{settings.CLOUDGYM_V2_BASE}{path}"
    headers = {"accept": "*/*", "Authorization": f"Bearer {token}"}
    resp = await _request_with_retry(_get_client(), "GET", url, headers=headers, params=params)
    return resp.json()


# ---------------- v1 (agendamento/aulas) ----------------

_CLASSES_CACHE_KEY = "cg:v1:classes"


async def list_classes(force: bool = False) -> list[dict]:
    """Catálogo de aulas da unidade. Paginado (chave 'content'). Cache Redis 1h.

    Retorna cada item com: id, name, time (HH:mm:ss), endTime, capacity, unit.
    """
    if not force:
        try:
            cached = await rds.cache_get(_CLASSES_CACHE_KEY)
            if cached:
                import json as _json
                return _json.loads(cached)
        except Exception:
            pass

    data = await _v1_get(f"/config/classes/{settings.CLOUDGYM_UNIT_ID}", params={"size": 500, "page": 0})
    if isinstance(data, list):
        items = data
    else:
        items = data.get("content") or data.get("items") or data.get("data") or []

    try:
        import json as _json
        await rds.cache_set(_CLASSES_CACHE_KEY, _json.dumps(items), ttl=3600)
    except Exception:
        pass
    return items


async def get_class_availability(data_yyyy_mm_dd: str, class_id: str) -> dict:
    return await _v1_get(f"/admin/classattendancelist/{settings.CLOUDGYM_UNIT_ID}/{data_yyyy_mm_dd}/{class_id}")


async def create_attendance_v2(memberid: int | str, date_yyyy_mm_dd: str, class_id: int | str) -> dict:
    """Cria agendamento de aula experimental.

    Endpoint: POST api.cloudgym.io/v1/classattendance (host v2 + token v2).
    Payload: {memberid, date, class_id}. Response: [{id: "..."}].
    """
    token = await _get_v2_token()
    url = f"{settings.CLOUDGYM_ATTENDANCE_BASE}/v1/classattendance"
    headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}
    payload = {
        "memberid": int(memberid),
        "date": date_yyyy_mm_dd,
        "class_id": int(class_id),
    }
    resp = await _request_with_retry(_get_client(), "POST", url, headers=headers, json=payload)
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


async def create_customer(name: str, phone_digits: str) -> dict:
    """Cadastra cliente novo (plano trial). Payload conforme n8n (fluxo n8n linhas 119-149)."""
    payload = {
        "name": name,
        "cellPhoneNumber": phone_digits,
        "planExtId": TRIAL_PLAN_EXT_ID,
        "installments": "1",
        "installreg": "1",
        "installman": "1",
        "methodPayment": "DN",
    }
    return await _v1_post("/customer", payload)


# ---------------- v2 (membros) ----------------

def format_phone_br(digits: str) -> str:
    """Normaliza celular BR para o formato aceito pela CloudGym.

    A v2 /v1/member?phone= aceita apenas dígitos (sem '+'). Telefones
    armazenados pela CloudGym têm 13 dígitos (DDI+DDD+9+numero). Se a entrada
    tiver 12 dígitos (DDD+numero sem o 9), insere o 9.

    Ex: 554132811234  -> 5541932811234
         5541998765432 -> 5541998765432 (inalterado)
    """
    only_digits = "".join(c for c in (digits or "") if c.isdigit())
    m = re.fullmatch(r"(\d{4})(\d{8})", only_digits)
    if m:
        return f"{m.group(1)}9{m.group(2)}"
    return only_digits


async def find_member_by_phone(phone_digits: str) -> list[dict]:
    """Busca membro por telefone. Tenta com e sem '+' (produção tem os dois formatos)."""
    formatted = format_phone_br(phone_digits)
    # Tenta sem '+' (formato usado ao cadastrar via create_customer)
    data = await _v2_get("/v1/member", params={"phone": formatted})
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    if items:
        return items
    # Fallback: com '+' (caso antigo / n8n)
    data = await _v2_get("/v1/member", params={"phone": f"+{formatted}"})
    items = data if isinstance(data, list) else (data.get("items") or data.get("data") or [])
    return items


async def find_member(query: str) -> list[dict]:
    """Busca livre (compat com followups/scheduler)."""
    data = await _v2_get("/v1/member", params={"search": query})
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("data") or []


async def list_all_members() -> list[dict]:
    data = await _v2_get("/v1/member")
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("data") or []


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:len(fmt)], fmt).date()
        except ValueError:
            continue
    return None


async def get_member_attendance(
    member_id: int | str, size: int = 1, sort: str = "date,desc"
) -> list[dict]:
    """Histórico de presença (aulas + musculação) de um membro.

    Endpoint v1: GET /customer/attendance/{memberId}?size=&page=&sort=
    Para pegar só a última presença, use size=1 e sort="date,desc".
    """
    data = await _v1_get(
        f"/customer/attendance/{member_id}",
        params={"size": size, "page": 0, "sort": sort},
    )
    if isinstance(data, list):
        return data
    return data.get("content") or data.get("items") or data.get("data") or []


async def list_members_expiring(days_before: int, reference: Optional[date] = None) -> list[dict]:
    """Retorna membros cuja data `enddate` cai em (hoje + days_before)."""
    ref = reference or date.today()
    target = ref + timedelta(days=days_before)
    members = await list_all_members()
    out = []
    for m in members:
        end = _parse_date(str(m.get("enddate", "")))
        if end == target:
            out.append(m)
    return out


async def list_members_birthday(reference: Optional[date] = None) -> list[dict]:
    """Retorna membros cuja data `birthday` bate com dia/mes do `reference` (default hoje)."""
    ref = reference or date.today()
    members = await list_all_members()
    out = []
    for m in members:
        b = _parse_date(str(m.get("birthday", "")))
        if b and (b.month, b.day) == (ref.month, ref.day):
            out.append(m)
    return out
