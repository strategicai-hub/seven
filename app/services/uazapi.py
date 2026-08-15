import base64
import asyncio
import json as _json
import logging

import httpx

from app.config import settings
from app.services import redis_service as rds

logger = logging.getLogger(__name__)

TRACK_SOURCE = "IA"

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30)
    return _client


def _headers() -> dict:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "token": settings.UAZAPI_TOKEN,
    }


def _json_body(payload: dict) -> bytes:
    return _json.dumps(payload, ensure_ascii=False).encode("utf-8")

# Backoff dos reenvios. Motivo: em 14/08/2026 a instancia caiu ("logged out from
# another device") entre o balao 1 e o 2 de uma resposta de 4 partes — a UAZAPI
# passou a devolver 503 e o lead ficou so com o cumprimento, sem a pergunta. Sem
# retry, qualquer indisponibilidade de segundos vira lead abandonado.
_RETRY_DELAYS = (2, 5, 12)


def _is_transient(exc: Exception) -> bool:
    """So repete o que tem chance real de dar certo na proxima tentativa.

    5xx = servidor UAZAPI/WhatsApp instavel ou instancia reconectando.
    Timeout/erro de conexao = rede. 4xx (numero invalido, token errado) nunca
    melhora com repeticao — falha na hora para o alerta subir rapido.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _post_with_retry(url: str, payload: dict, what: str, number: str) -> "httpx.Response":
    """POST na UAZAPI com reenvio automatico em falha transitoria."""
    client = _get_client()
    last: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            resp = await client.post(url, content=_json_body(payload), headers=_headers())
            resp.raise_for_status()
            if attempt:
                logger.info("%s para %s enviado na tentativa %d", what, number, attempt + 1)
            return resp
        except Exception as exc:  # noqa: BLE001 - reclassificado logo abaixo
            last = exc
            if attempt >= len(_RETRY_DELAYS) or not _is_transient(exc):
                break
            delay = _RETRY_DELAYS[attempt]
            logger.warning(
                "Falha ao enviar %s para %s (tentativa %d/%d): %s — retentando em %ds",
                what, number, attempt + 1, len(_RETRY_DELAYS) + 1, exc, delay,
            )
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


async def _remember_outbound(data: dict) -> None:
    msg_id = data.get("id") or data.get("messageid") or ""
    await rds.mark_outbound_id(msg_id)


async def send_text(number: str, text: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/text"
    payload = {"number": number, "text": text, "delay": delay, "track_source": TRACK_SOURCE}
    await rds.mark_outbound_echo(number, text)
    resp = await _post_with_retry(url, payload, "texto", number)
    data = resp.json()
    await _remember_outbound(data)
    logger.info("Texto enviado para %s", number)
    return data


async def send_presence(number: str, presence: str = "composing") -> None:
    """Emite presenca (digitando...). Endpoint correto: POST /message/presence
    com {number, presence}. NAO usar /chat/presence nem /send/presence (405)."""
    url = f"{settings.UAZAPI_BASE_URL}/message/presence"
    payload = {"number": number, "presence": presence}
    try:
        client = _get_client()
        resp = await client.post(url, content=_json_body(payload), headers=_headers())
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Falha ao enviar presence %s para %s: %s", presence, number, e)


async def mark_read(number: str) -> None:
    """Marca o chat como lido (tiques azuis). Endpoint: POST /chat/read {number}."""
    url = f"{settings.UAZAPI_BASE_URL}/chat/read"
    payload = {"number": number}
    try:
        client = _get_client()
        resp = await client.post(url, content=_json_body(payload), headers=_headers())
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Falha ao marcar chat lido para %s: %s", number, e)


async def _send_media(number: str, media_type: str, file_url: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/media"
    payload = {
        "number": number,
        "type": media_type,
        "file": file_url,
        "delay": delay,
        "track_source": TRACK_SOURCE,
    }
    resp = await _post_with_retry(url, payload, media_type, number)
    data = resp.json()
    await _remember_outbound(data)
    logger.info("%s enviado para %s", media_type, number)
    return data


async def send_image(number: str, image_url: str, caption: str = "") -> dict:
    return await _send_media(number, "image", image_url)


async def send_document(number: str, document_url: str, filename: str = "arquivo.pdf") -> dict:
    return await _send_media(number, "document", document_url)


async def send_video(number: str, video_url: str, caption: str = "") -> dict:
    return await _send_media(number, "video", video_url)


async def get_instance_status() -> dict:
    """Estado da instancia WhatsApp (connected / disconnected / connecting).

    Usado pelo vigia de conexao. Quando o aparelho e deslogado ("logged out from
    another device"), /send/text passa a responder 503 e o bot fica mudo sem que
    ninguem perceba — este endpoint e a unica forma de detectar isso sozinho.
    """
    url = f"{settings.UAZAPI_BASE_URL}/instance/status"
    client = _get_client()
    resp = await client.get(url, headers=_headers())
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("instance") or data

async def download_media(media_url: str) -> bytes:
    client = _get_client()
    resp = await client.get(media_url, headers=_headers())
    resp.raise_for_status()
    return resp.content


async def download_media_by_id(messageid: str) -> bytes:
    url = f"{settings.UAZAPI_BASE_URL}/message/download"
    payload = {"id": messageid, "return_base64": "true"}
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
    data = resp.json()
    b64 = data.get("base64Data") or ""
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    if b64:
        return base64.b64decode(b64)
    # Fallback: alguns retornos podem trazer só fileURL (sem base64).
    file_url = data.get("fileURL") or ""
    if file_url:
        r = await client.get(file_url, headers=_headers())
        r.raise_for_status()
        return r.content
    raise RuntimeError(f"resposta sem base64Data nem fileURL: keys={list(data.keys())}")
