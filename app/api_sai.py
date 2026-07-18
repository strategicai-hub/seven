"""Endpoints chamados pelo SAI Comercial.

POST /sai/bind     — super admin vinculou (ou desvinculou) este chatbot a um
                     tenant. Body: {tenantSlug, ingestSecret} ou
                     {tenantSlug: null, ingestSecret: null}. Autenticado por
                     SAI_REGISTRATION_TOKEN (mesmo segredo do auto-registro).

POST /sai/config   — push do snapshot do painel (assistant + products).
                     Autenticado por ingest_secret do binding atual.

POST /sai/history  — grava no historico Redis uma mensagem avulsa (atendente
                     humano ou lead) sem gerar resposta da IA. Autenticado por
                     ingest_secret do binding atual.

Contrato detalhado em sai-comercial/docs/painel-ia-sync.md.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.services import redis_service, sai_sync

logger = logging.getLogger(__name__)
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


@router.post("/history")
async def push_history(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Acumula no historico Redis uma mensagem que o bot nao veria, sem gerar
    resposta da IA.

    Chamado fire-and-forget pelo SAI Comercial quando o gate de pausa suprime o
    relay do inbound ao bot (IA pausada — o lead escreveu durante o atendimento
    humano) ou quando o provider nao tem eco fromMe (API Oficial Meta — mensagem
    da atendente). role="attendant" grava como fala do bot (model); role="lead"
    grava como fala do lead (user). Assim, ao religar a IA, o bot retoma a
    conversa com o contexto completo em vez de se reapresentar do zero.
    Autenticado por ingest_secret do binding atual (mesmo esquema do /config).
    """
    cfg = await sai_sync._active_config_async()
    if not cfg:
        raise HTTPException(status_code=503, detail="sync nao configurado (sem binding)")
    _expected_slug, expected_secret = cfg
    if not x_ingest_secret or not hmac.compare_digest(x_ingest_secret, expected_secret):
        raise HTTPException(status_code=401, detail="invalid secret")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="payload invalido")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload deve ser objeto")

    phone = re.sub(r"\D+", "", str(payload.get("phone") or ""))
    content = str(payload.get("content") or "").strip()
    if not phone or not content:
        raise HTTPException(status_code=400, detail="phone e content obrigatorios")

    # append_chat_history grava role "model" como fala do bot (type=ai) e
    # qualquer outro role como fala do lead (type=human).
    role = "model" if payload.get("role") == "attendant" else "user"
    await redis_service.append_chat_history(phone, role, content)
    logger.info(
        "api_sai: /history registrou %s (%d chars) para %s",
        payload.get("role"), len(content), phone,
    )
    return {"ok": True, "phone": phone, "role": payload.get("role")}
