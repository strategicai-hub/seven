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
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import redis_service as rds

logger = logging.getLogger(__name__)

_V1_TOKEN_KEY = "cg:v1:token"
_V2_TOKEN_KEY = "cg:v2:token"

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        kwargs: dict[str, Any] = {"timeout": 30}
        if settings.CLOUDGYM_PROXY:
            kwargs["proxy"] = settings.CLOUDGYM_PROXY
        _client = httpx.AsyncClient(**kwargs)
    return _client


async def _get_v1_token() -> str:
    cached = await rds.cache_get(_V1_TOKEN_KEY)
    if cached:
        return cached

    url = f"{settings.CLOUDGYM_V1_BASE}/auth/token"
    headers = {
        "accept": "*/*",
        "Authorization": f"Basic {settings.CLOUDGYM_V1_BASIC}",
    }
    client = _get_client()
    resp = await _request_with_retry(client, "POST", url, headers=headers)
    data = resp.json()
    token = data.get("access_token") or data.get("token") or ""
    expires_in = int(data.get("expires_in") or 600)
    if token:
        await rds.cache_set(_V1_TOKEN_KEY, token, ttl=max(60, expires_in - 30))
    return token


async def _get_v2_token() -> str:
    cached = await rds.cache_get(_V2_TOKEN_KEY)
    if cached:
        return cached

    url = f"{settings.CLOUDGYM_V2_BASE}/auth"
    payload = {
        "username": settings.CLOUDGYM_V2_USERNAME,
        "password": settings.CLOUDGYM_V2_PASSWORD,
    }
    client = _get_client()
    resp = await _request_with_retry(client, "POST", url, json=payload)
    data = resp.json()
    token = data.get("token") or data.get("access_token") or ""
    expires_in = int(data.get("expires_in") or 3600)
    if token:
        await rds.cache_set(_V2_TOKEN_KEY, token, ttl=max(60, expires_in - 30))
    return token


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    json: Any = None,
    max_retries: int = 3,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(method, url, headers=headers, params=params, json=json)
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

async def list_classes() -> list[dict]:
    data = await _v1_get(f"/config/classes/{settings.CLOUDGYM_UNIT_ID}")
    if isinstance(data, list):
        return data
    return data.get("items") or data.get("data") or []


async def get_class_availability(data_yyyy_mm_dd: str, class_id: str) -> dict:
    return await _v1_get(f"/admin/classattendancelist/{settings.CLOUDGYM_UNIT_ID}/{data_yyyy_mm_dd}/{class_id}")


async def create_attendance(payload: dict) -> dict:
    return await _v1_post("/v1/classattendance", payload)


async def create_customer(payload: dict) -> dict:
    return await _v1_post("/customer", payload)


# ---------------- v2 (membros) ----------------

async def find_member(query: str) -> list[dict]:
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
