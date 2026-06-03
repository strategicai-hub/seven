"""Endpoints chamados pelo SAI Comercial.

POST /sai/bind     — super admin vinculou (ou desvinculou) este chatbot a um
                     tenant. Body: {tenantSlug, ingestSecret} ou
                     {tenantSlug: null, ingestSecret: null}. Autenticado por
                     SAI_REGISTRATION_TOKEN (mesmo segredo do auto-registro).

POST /sai/config   — push do snapshot do painel (assistant + products).
                     Autenticado por ingest_secret do binding atual.

Contrato detalhado em sai-comercial/docs/painel-ia-sync.md.
"""
from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.services import sai_sync

router = APIRouter(prefix=f"{settings.WEBHOOK_PATH}/sai")


@router.post("/bind")
async def receive_bind(
    request: Request,
    x_registration_token: str | None = Header(default=None, alias="x-registration-token"),
):
    expected = settings.SAI_REGISTRATION_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="registro nao configurado")
    if not x_registration_token or not hmac.compare_digest(x_registration_token, expected):
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload invalido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload deve ser objeto")
    tenant_slug = payload.get("tenantSlug")
    ingest_secret = payload.get("ingestSecret")
    if tenant_slug is None and ingest_secret is None:
        await sai_sync.clear_binding()
        return {"ok": True, "bound": False}
    if not isinstance(tenant_slug, str) or not isinstance(ingest_secret, str):
        raise HTTPException(status_code=400, detail="tenantSlug e ingestSecret obrigatorios")
    await sai_sync.save_binding(tenant_slug.strip(), ingest_secret.strip())
    # Dispara sync imediato em background — nao bloqueia o response.
    asyncio.create_task(sai_sync.sync_now())
    return {"ok": True, "bound": True, "tenantSlug": tenant_slug}


@router.post("/config")
async def receive_config(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    cfg = await sai_sync._active_config_async()
    if not cfg:
        raise HTTPException(status_code=503, detail="sync nao configurado (sem binding)")
    expected_slug, expected_secret = cfg
    if not x_ingest_secret or not hmac.compare_digest(x_ingest_secret, expected_secret):
        raise HTTPException(status_code=401, detail="invalid secret")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload invalido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload deve ser objeto")
    if payload.get("tenantSlug") != expected_slug:
        raise HTTPException(status_code=400, detail="tenantSlug nao confere")
    await sai_sync.save_snapshot(payload)
    return {"ok": True}
