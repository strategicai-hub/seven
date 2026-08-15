"""
RabbitMQ -> Gemini (com tools) -> UAZAPI.
Consome mensagens da fila `seven`, aplica debounce, chama Gemini com function
calling, envia respostas e persiste estado do lead no SQLite.
"""
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis as redis_sync

from app import db
from app.config import settings
from app.images import MEDIA_DICT
from app.services import redis_service as rds
from app.services import sheets_service, telegram, uazapi
from app.services.gemini import (
    analyze_image,
    chat_with_tools,
    generate_summary,
    transcribe_audio,
)
from app.services.rabbitmq import consume
from app.tools import handle_atendimento_humano

logger = logging.getLogger(__name__)

TEXT_TYPES = {"ExtendedTextMessage", "Conversation", "ContactMessage"}

_LOG_KEY = "seven:logs"
try:
    _log_redis = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)
    _log_redis.ping()
except Exception:
    _log_redis = None

_session_log: list[str] = []


def _msg(t: str) -> str: return f'<span style="color:#3498db"><b>📩 MSG</b></span> {t}'
def _ai(t: str) -> str: return f'<span style="color:#9b59b6"><b>🤖 IA</b></span> {t}'
def _ok(t: str) -> str: return f'<span style="color:#27ae60"><b>✅ OK</b></span> {t}'
def _warn(t: str) -> str: return f'<span style="color:#e67e22"><b>⚠️</b></span> {t}'
def _err(t: str) -> str: return f'<span style="color:#e74c3c"><b>❌</b></span> {t}'
def _human(origem: str, t: str) -> str: return f'<span style="color:#16a085"><b>🧑‍💼 ATENDENTE HUMANO - {origem}</b></span> {t}'


def _strip_html(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t)



def _is_reset_confirmation(text: str) -> bool:
    normalized = " ".join((text or "").split()).casefold().rstrip(".!")
    return normalized == "conversa reiniciada"


def log(line: str) -> None:
    logger.info(_strip_html(line))
    _session_log.append(line)


def _save_session_log(phone: str) -> None:
    global _session_log
    if _log_redis and _session_log:
        entry = json.dumps(
            {"ts": time.time(), "phone": phone, "lines": list(_session_log)},
            ensure_ascii=False,
        )
        _log_redis.lpush(_LOG_KEY, entry)
        _log_redis.ltrim(_LOG_KEY, 0, 499)
    _session_log = []


def _is_group(chat_id: str) -> bool:
    return "@g.us" in chat_id


def _next_followup_iso(days: int = 1) -> str:
    """Próximo follow-up: daqui a `days` dias entre 08:00 e 08:59 SP, em ISO UTC.
    Minuto aleatório para distribuir a carga do reactivation a cada 1min."""
    tz = ZoneInfo(settings.SCHEDULER_TZ)
    target = (datetime.now(tz) + timedelta(days=days)).replace(
        hour=8, minute=random.randint(0, 59), second=0, microsecond=0
    )
    return target.astimezone(timezone.utc).isoformat()


def _parse_ai_response(text: str) -> tuple[list[dict], bool, int | None]:
    finalizado = False
    match = re.search(r"\[FINALIZADO=(\d)\]", text)
    if match:
        finalizado = match.group(1) == "1"
        text = re.sub(r"\[FINALIZADO=\d\]", "", text).strip()

    pensar_days: int | None = None
    pmatch = re.search(r"\[PENSAR=(\d+)\]", text)
    if pmatch:
        pensar_days = int(pmatch.group(1))
        text = re.sub(r"\[PENSAR=\d+\]", "", text).strip()

    if "|||" in text:
        raw_parts = [p.strip() for p in text.split("|||") if p.strip()]
    else:
        raw_parts = [p.strip() for p in text.split("\n\n") if p.strip()]

    parts: list[dict] = []
    for part in raw_parts:
        tag_match = re.search(r"\[([A-Z_0-9]+)\]", part)
        tag_key = f"[{tag_match.group(1)}]" if tag_match else None
        if tag_key and tag_key in MEDIA_DICT:
            media = MEDIA_DICT[tag_key]
            parts.append({"type": media["type"], "content": media["url"]})
        else:
            clean = re.sub(r"\[(FINALIZADO|PENSAR|IMAGEM)[^\]]*\]", "", part).strip()
            if clean:
                parts.append({"type": "text", "content": clean})

    if not parts and text.strip():
        parts = [{"type": "text", "content": text.strip()}]

    return parts, finalizado, pensar_days


async def _process_message(msg: dict) -> None:
    phone = msg.get("phone", "")
    chat_id = msg.get("chat_id", "")
    from_me = msg.get("from_me", False)
    msg_type = msg.get("msg_type", "")
    msg_text = msg.get("msg", "")
    push_name = msg.get("push_name", "")

    log(_msg(f"[{phone}] [RECV] type={msg_type} from_me={from_me}"))

    if not phone or msg_type in ("", "Unknown"):
        log(_warn(f"[{phone}] ignorado: type={msg_type!r}"))
        _save_session_log(phone)
        return

    if from_me and msg_type in TEXT_TYPES and _is_reset_confirmation(msg_text):
        logger.info("Eco de confirmacao de reset ignorado para %s", phone)
        _save_session_log(phone)
        return

    if from_me:
        await rds.set_block(phone)
        # Origem: painel SAI envia via API (wasSentByApi=true); WhatsApp do
        # celular/Web nao seta a flag.
        raw = msg.get("raw_message") or {}
        via_painel = bool(raw.get("wasSentByApi"))
        origem = "Painel SAI" if via_painel else "WhatsApp"
        # Grava a mensagem da atendente no historico do Gemini (role "model",
        # como se o bot tivesse dito). Sem isso a Zoe retoma a conversa sem
        # saber o que a humana falou e se reapresenta do zero ao ser liberada.
        # Ecos da propria IA nao chegam aqui — o webhook ja filtra por
        # track_source e por id outbound registrado no envio.
        if msg_type in TEXT_TYPES and msg_text:
            await rds.append_chat_history(phone, "model", msg_text)
        elif msg_type == "AudioMessage":
            await rds.append_chat_history(phone, "model", "[Audio enviado pelo atendente humano]")
        elif msg_type == "ImageMessage":
            caption = msg.get("caption", "")
            texto = "[Imagem enviada pelo atendente humano]"
            if caption:
                texto += f" Legenda: {caption}"
            await rds.append_chat_history(phone, "model", texto)
        log(_human(origem, f"[{phone}] agente bloqueado ate amanha 08:00 SP"))
        logger.info("Humano assumiu chat %s via %s - agente bloqueado ate amanha 08:00 SP", chat_id, origem)
        _save_session_log(phone)
        return

    if await rds.is_blocked(phone):
        if await rds.clear_stale_legacy_block(phone):
            log(_warn(f"[{phone}] bloqueio legado vazio removido"))
        else:
            # Acumula a mensagem do lead no historico mesmo com o bot calado —
            # quando a Zoe for liberada, retoma com o contexto completo do
            # atendimento humano em vez de um buraco na conversa.
            if msg_type in TEXT_TYPES and msg_text:
                await rds.append_chat_history(phone, "user", msg_text)
            elif msg_type == "AudioMessage":
                await rds.append_chat_history(phone, "user", "[Audio recebido durante atendimento humano]")
            elif msg_type == "ImageMessage":
                caption = msg.get("caption", "")
                texto = "[Imagem recebida durante atendimento humano]"
                if caption:
                    texto += f" Legenda: {caption}"
                await rds.append_chat_history(phone, "user", texto)
            log(_warn(f"[{phone}] agente bloqueado (humano ativo) — msg registrada no historico"))
            _save_session_log(phone)
            return

    if _is_group(chat_id):
        log(_warn(f"[{phone}] ignorado (grupo): chat_id={chat_id!r}"))
        _save_session_log(phone)
        return

    # Mensagem inbound valida confirmada — marca como lida (tiques azuis).
    await uazapi.mark_read(phone)

    # Comando /reset — precedência MÁXIMA (roda antes de modo_mudo para
    # garantir que seja sempre possível sair de um atendimento humano).
    # Apaga TUDO relacionado ao número: histórico, buffer, bloqueio humano,
    # alertas, dedups de follow-up e a row do SQLite (nome, modo_mudo, status,
    # follow-up, dia_aula).
    if msg_type in TEXT_TYPES and (msg_text or "").strip().lower() == "/reset":
        await rds.reset_lead_state(phone)
        await db.delete_lead(phone)
        log(_ok(f"[{phone}] Reset solicitado"))
        try:
            await uazapi.send_text(phone, "Conversa reiniciada.")
        except Exception as e:
            log(_err(f"[{phone}] Falha ao confirmar reset: {e}"))
        _save_session_log(phone)
        return

    # Modo mudo: após atendimento_humano, pula Gemini e responde [FINALIZADO=1] local
    if await db.is_modo_mudo(phone):
        log(_warn(f"[{phone}] modo_mudo ativo — silêncio (atendimento humano assumiu)"))
        _save_session_log(phone)
        return

    # Upsert do lead em SQLite — nome só é gravado via tool salva_nome.
    # push_name (nome do contato no WhatsApp) é usado apenas como fallback
    # para alertas/Sheets, nunca como nome confirmado para o Gemini.
    lead = await db.get_lead(phone)
    if lead is None:
        await db.upsert_lead(phone)
        lead = await db.get_lead(phone) or {}

    media_url = msg.get("media_url", "")
    messageid = msg.get("messageid", "")
    if msg_type in TEXT_TYPES:
        buffer_text = msg_text
    elif msg_type == "ReactionMessage":
        # Enriquecemos o emoji com contexto explícito — o Gemini diferencia
        # 🙏/❤️/👍 mas precisa saber que é uma reação ao último envio dele,
        # não uma mensagem nova solta.
        emoji = (msg_text or "").strip() or "(sem emoji)"
        buffer_text = f"[Reação do lead ao último envio: {emoji}]"
    elif msg_type == "AudioMessage":
        log(f"[AUDIO] transcribe_audio(phone={phone})")
        try:
            if media_url:
                audio_bytes = await uazapi.download_media(media_url)
            elif messageid:
                audio_bytes = await uazapi.download_media_by_id(messageid)
            else:
                audio_bytes = None
            if audio_bytes:
                transcription = await transcribe_audio(audio_bytes)
                buffer_text = f"[Áudio transcrito]: {transcription}"
                log(_ok(f"[AUDIO] transcrito ({len(transcription)} chars)"))
            else:
                buffer_text = "[Áudio recebido — sem URL nem messageid]"
                log(_warn(f"[AUDIO] sem media_url nem messageid"))
        except Exception as e:
            log(_err(f"[AUDIO] {e}"))
            buffer_text = "[Áudio recebido — erro na transcrição]"
    elif msg_type == "ImageMessage":
        log(f"[IMG] analyze_image(phone={phone})")
        try:
            caption = msg.get("caption", "")
            if media_url:
                image_bytes = await uazapi.download_media(media_url)
            elif messageid:
                image_bytes = await uazapi.download_media_by_id(messageid)
            else:
                image_bytes = None
            if image_bytes:
                description = await analyze_image(image_bytes)
                buffer_text = f"[Imagem recebida]: {description}"
                if caption:
                    buffer_text += f"\nLegenda: {caption}"
                log(_ok(f"[IMG] analisada"))
            else:
                buffer_text = "[Imagem recebida — sem URL nem messageid]"
        except Exception as e:
            log(_err(f"[IMG] {e}"))
            buffer_text = "[Imagem recebida — erro na análise]"
    else:
        buffer_text = msg_text or f"[Mensagem tipo {msg_type}]"

    if not buffer_text:
        return

    # Debounce
    count = await rds.push_buffer(phone, buffer_text)
    if count > 1:
        logger.info("Buffer já ativo para %s (count=%d)", phone, count)
        return

    if phone not in settings.debounce_bypass_phones_set:
        await asyncio.sleep(settings.DEBOUNCE_SECONDS)

    messages = await rds.get_buffer(phone)
    await rds.delete_buffer(phone)

    unified_msg = "\n".join(messages)
    log(_msg(f"[{phone} - {push_name}] {unified_msg[:300]}"))

    # Re-checa modo mudo (pode ter mudado durante o debounce)
    if await db.is_modo_mudo(phone):
        log(_warn(f"[{phone}] modo_mudo ativo pós-debounce"))
        _save_session_log(phone)
        return

    # Emite "digitando..." antes de iniciar o processamento com a IA.
    await uazapi.send_presence(phone, "composing")

    # Chamada ao Gemini com function calling
    log(f"[GEMINI] chat_with_tools(phone={phone}, msg_len={len(unified_msg)})")
    try:
        ai_response, tokens = await chat_with_tools(
            phone, unified_msg, lead_name=(lead.get("nome") or "")
        )
    except Exception as e:
        # Gemini indisponível após todos os retries (sobrecarga, timeout, etc).
        # Transferimos silenciosamente para a recepção com motivo neutro. Nem o
        # lead nem o cliente (recepção) podem perceber a falha técnica.
        log(_err(f"[GEMINI] falha definitiva: {e} — transferindo silenciosamente"))
        try:
            await uazapi.send_text(
                phone,
                "Só um momento! 😊 Vou chamar alguém da nossa recepção "
                "pra te atender por aqui, tá bom?",
            )
        except Exception as e2:
            log(_err(f"[FALLBACK] falha ao enviar msg ao lead: {e2}"))
        try:
            await handle_atendimento_humano(
                phone,
                {"motivo": "Cliente solicitou falar com a equipe"},
            )
        except Exception as e2:
            log(_err(f"[FALLBACK] falha ao acionar atendimento humano: {e2}"))
        _save_session_log(phone)
        return

    if not ai_response:
        # Camada 4 — cinto de segurança. chat_with_tools já devia garantir
        # resposta não-vazia; se chegou aqui, algo grave quebrou.
        # Transferimos silenciosamente para a recepção humana com motivo neutro:
        # nem o lead nem a recepção (cliente) devem perceber que houve falha
        # técnica. A falha fica registrada apenas nos logs internos do worker.
        log(_err(
            f"[GEMINI] resposta vazia pós-fallback — transferindo silenciosamente. "
            f"Última msg do lead: {unified_msg[:140]}"
        ))
        try:
            await uazapi.send_text(
                phone,
                "Só um momento! 😊 Vou chamar alguém da nossa recepção "
                "pra te atender por aqui, tá bom?",
            )
        except Exception as e:
            log(_err(f"[FALLBACK] falha ao enviar msg ao lead: {e}"))
        try:
            await handle_atendimento_humano(
                phone,
                {"motivo": "Cliente solicitou falar com a equipe"},
            )
        except Exception as e:
            log(_err(f"[FALLBACK] falha ao acionar atendimento humano: {e}"))
        _save_session_log(phone)
        return

    if await rds.is_blocked(phone):
        log(_warn(f"[{phone}] Humano assumiu durante processamento"))
        _save_session_log(phone)
        return

    # Abort por nova mensagem: se o lead mandou algo ENQUANTO o Gemini estava
    # processando, o buffer foi repopulado (o outro ciclo iniciou). Descartamos
    # esta resposta (evita responder fora de contexto), removemos user+model
    # fantasmas do histórico e reinjetamos a unified_msg no início do buffer
    # para que o novo ciclo processe tudo junto.
    if await rds.get_buffer(phone):
        log(_warn(f"[{phone}] nova msg durante Gemini — descartando resposta e mesclando ciclos"))
        await rds.pop_last_history(phone, n=2)
        await rds.prepend_buffer(phone, unified_msg)
        _save_session_log(phone)
        return

    parts, finalizado, pensar_days = _parse_ai_response(ai_response)
    log(_ok(f"[GEMINI] {len(parts)} parte(s), finalizado={finalizado} pensar_days={pensar_days}"))
    log(_ai(f"[{phone}] {ai_response[:400]}"))
    if tokens[2]:
        log(f"[TOKENS] prompt={tokens[0]} output={tokens[1]} total={tokens[2]}")

    for i, part in enumerate(parts):
        try:
            if part["type"] == "text":
                # "digitando..." antes de cada bloco de texto, com pausa curta.
                await uazapi.send_presence(phone, "composing")
                await asyncio.sleep(1.5)
                await uazapi.send_text(phone, part["content"])
            elif part["type"] == "image":
                await uazapi.send_image(phone, part["content"])
                await asyncio.sleep(3)
            elif part["type"] == "document":
                await uazapi.send_document(phone, part["content"])
            elif part["type"] == "video":
                await uazapi.send_video(phone, part["content"])
        except Exception as e:
            # uazapi ja tentou reenviar (backoff 2s/5s/12s). Se chegou aqui, o
            # canal esta fora mesmo. Seguir para os baloes seguintes entregaria
            # a resposta picada e fora de ordem — melhor parar e avisar a equipe.
            faltando = len(parts) - i
            log(_err(
                f"[WHATSAPP] falha ao enviar {part['type']} ({i+1}/{len(parts)}): {e}. "
                f"Abortando o restante da resposta ({faltando} parte(s) nao entregue(s))."
            ))
            logger.exception("Erro ao enviar %s para %s", part["type"], phone)
            await _alert_delivery_failure(phone, i + 1, len(parts), parts, e)
            break

    if pensar_days and pensar_days > 0:
        # [PENSAR=N] — lead reafirmou que precisa de tempo. Não finaliza:
        # agenda follow-up suave em N dias (default 3), reset de stage para 1.
        try:
            await db.schedule_followup(phone, _next_followup_iso(days=pensar_days), stage=1)
            log(_ok(f"[{phone}] Lead em modo 'pensar' — follow-up agendado em {pensar_days}d"))
        except Exception as e:
            log(_warn(f"[{phone}] falha ao agendar follow-up de pensar: {e}"))
    elif finalizado:
        # [FINALIZADO=1] encerra o fluxo e desliga follow-up, mas NÃO bloqueia
        # a IA — o lead ainda pode mandar agradecimento/dúvida e a Zoe responde.
        # Bloqueio de 1h só é ativado quando humano assume (from_me=True).
        await db.mark_finalizado(phone)
        log(_ok(f"[{phone}] Conversa finalizada (follow-up desligado, IA ainda responde)"))
    else:
        # Reagenda follow-up de reativação para amanhã 08:00–08:59 SP e reseta
        # o stage para 1. Sempre que o lead responde, zeramos a contagem de
        # tentativas — o scheduler de reactivation só dispara se o lead ficar
        # em silêncio até amanhã de manhã.
        try:
            await db.schedule_followup(phone, _next_followup_iso(), stage=1)
        except Exception as e:
            log(_warn(f"[{phone}] falha ao reagendar follow-up: {e}"))

    conversa_encerrada = finalizado or await db.is_modo_mudo(phone)
    await _update_summary_and_sheets(phone, lead.get("nome") or push_name, conversa_encerrada)
    _save_session_log(phone)


async def _alert_delivery_failure(
    phone: str,
    parte: int,
    total: int,
    parts: list,
    erro: Exception,
) -> None:
    """Avisa a equipe que um lead ficou sem resposta por falha de entrega.

    Vai por Telegram (canal independente — funciona mesmo com o WhatsApp fora)
    e tambem por WhatsApp, que so chega em falha parcial. O log em ERROR vem
    primeiro para o caso de nenhum dos dois canais estar configurado.
    """
    nao_entregue = " | ".join(
        str(p.get("content", ""))[:160] for p in parts[parte - 1:] if p.get("type") == "text"
    )
    logger.error(
        "ENTREGA FALHOU: lead %s recebeu %d de %d parte(s). Nao entregue: %s",
        phone, parte - 1, total, nao_entregue[:500],
    )
    alert_text = (
        "⚠️ RESPOSTA NAO ENTREGUE\n"
        f"WhatsApp: {phone}\n"
        f"Entregue: {parte - 1} de {total} mensagem(ns)\n"
        f"Erro: {str(erro)[:180]}\n\n"
        "O lead ficou sem resposta completa — assumir a conversa manualmente."
    )
    await telegram.send_alert(alert_text)
    if not settings.ALERT_PHONE:
        return
    try:
        await uazapi.send_text(settings.ALERT_PHONE, alert_text)
    except Exception as alert_error:  # noqa: BLE001
        logger.warning("Nao foi possivel avisar a equipe por WhatsApp: %s", alert_error)


async def _update_summary_and_sheets(phone: str, name: str | None, gerar_resumo: bool = False) -> None:
    resumo = ""
    if gerar_resumo:
        try:
            resumo = await generate_summary(phone)
        except Exception:
            resumo = ""
    try:
        sheets_service.upsert_lead(phone=phone, name=name or "", resumo=resumo or "")
    except Exception:
        logger.exception("Erro ao atualizar sheets %s", phone)


async def start_consumer() -> None:
    await db.init_db()
    await consume(_process_message)
