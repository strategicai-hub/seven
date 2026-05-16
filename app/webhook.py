"""
Webhook -> RabbitMQ: recebe mensagens do WhatsApp (UAZAPI), filtra e publica na fila.
"""
import json
import logging

from fastapi import APIRouter, Request

from app import db
from app.config import settings
from app.services import redis_service as rds, uazapi
from app.services.rabbitmq import publish

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(settings.WEBHOOK_PATH)
async def webhook(request: Request):
    payload = await request.json()

    msg = payload.get("message", {})

    track_source = msg.get("track_source", "")
    if track_source in ("n8n", "IA"):
        return {"status": "ignored", "reason": f"track_source={track_source}"}

    from_me = msg.get("fromMe", False)

    # Quando fromMe=True (atendente humano enviou pelo WhatsApp Web/celular),
    # sender_pn é o número DA EMPRESA e chatid é o do LEAD (destinatário).
    # Precisamos do número do lead para bloquear a Zoe corretamente.
    if from_me:
        raw_sender = msg.get("chatid") or msg.get("sender_pn") or msg.get("sender", "")
    else:
        raw_sender = msg.get("sender_pn") or msg.get("chatid") or msg.get("sender", "")
    phone = raw_sender.split("@")[0] if raw_sender else ""
    chat_id = msg.get("chatid") or raw_sender
    push_name = msg.get("senderName", "")

    text = msg.get("text", "")
    msg_type_raw = msg.get("messageType", "")
    msg_type_norm = msg_type_raw.lower() if msg_type_raw else ""

    # Reaction detectada antes do text bruto — UAZAPI entrega o emoji em `text`,
    # e sem este branch a reação viraria uma "Conversation" qualquer (bug que
    # disparou nova abertura de conversa enquanto humano atendia).
    if msg_type_norm == "reactionmessage" or "reactionMessage" in msg:
        msg_type = "ReactionMessage"
        media_url = ""
        caption = ""
    elif text:
        msg_type = "Conversation"
        media_url = ""
        caption = ""
    elif msg_type_norm == "audiomessage" or "audioMessage" in msg:
        msg_type = "AudioMessage"
        media_url = msg.get("mediaUrl") or msg.get("url", "")
        caption = ""
    elif msg_type_norm == "imagemessage" or "imageMessage" in msg:
        msg_type = "ImageMessage"
        media_url = msg.get("mediaUrl") or msg.get("url", "")
        caption = msg.get("caption", "")
    else:
        msg_type = "Unknown"
        media_url = ""
        caption = ""

    if not phone or msg_type == "Unknown":
        logger.warning(
            "Webhook ignorado (phone=%r, msg_type=%r). Payload bruto: %s",
            phone, msg_type, json.dumps(payload)[:2000],
        )
        return {"status": "ignored", "reason": "no phone or unsupported message"}

    if phone in settings.blocked_sender_phones_set:
        logger.info("Mensagem de %s ignorada (BLOCKED_SENDER_PHONES)", phone)
        return {"status": "ignored", "reason": "phone blocked"}

    allowed = settings.allowed_phones_list
    if allowed and phone not in allowed:
        logger.info("Mensagem de %s ignorada (fora da whitelist ALLOWED_PHONES)", phone)
        return {"status": "ignored", "reason": "phone not in whitelist"}

    # /reset instantâneo — processa antes da fila para não esperar debounce
    text_normalized = (text or "").strip().lower()
    if text_normalized == "/reset":
        await rds.clear_chat_history(phone)
        await db.upsert_lead(phone, nome=None, modo_mudo=0, status_conversa="novo",
                             next_follow_up=None, stage_follow_up=0, dia_aula=None)
        await rds.delete_buffer(phone)
        try:
            await uazapi.send_text(phone, "Conversa reiniciada.")
        except Exception as e:
            logger.error("[%s] Falha ao confirmar reset: %s", phone, e)
        logger.info("[%s] Reset instantâneo via webhook", phone)
        return {"status": "reset"}

    queue_message = {
        "phone": phone,
        "push_name": push_name,
        "from_me": from_me,
        "msg_type": msg_type,
        "msg": text,
        "chat_id": chat_id,
        "media_url": media_url,
        "messageid": msg.get("messageid") or msg.get("id", ""),
        "caption": caption,
        "raw_message": msg,
    }

    await publish(queue_message)
    logger.info("Mensagem de %s publicada na fila", phone)
    return {"status": "queued"}
