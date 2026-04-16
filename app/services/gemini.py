"""
Wrapper do Gemini 2.5 Flash usando o novo SDK `google-genai`.

- chat_with_tools: conversa com function calling, usando o prompt modular (PROMPT_CORE
  + catálogos sob demanda). Envia o system_instruction sempre como primeiro bloco
  para ativar o cache implícito do Gemini 2.5 Flash (≈75% de desconto nos tokens
  cacheados a partir da segunda chamada em <=5 min).
- transcribe_audio / analyze_image: utilitários para mídia.
- call_with_retry: wrapper que detecta sobrecarga (503, 429, "overloaded") e
  refaz a chamada com backoff exponencial + jitter (máx 6 tentativas).
"""
import asyncio
import logging
import random
import re
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as gtypes

from app.config import settings
from app.prompt import build_system_prompt
from app.services.redis_service import append_chat_history, get_chat_history
from app.tools import ALL_TOOLS, dispatch


_DAYS_PT = {
    0: "segunda-feira", 1: "terça-feira", 2: "quarta-feira",
    3: "quinta-feira", 4: "sexta-feira", 5: "sábado", 6: "domingo",
}


def _current_time_sp() -> datetime:
    """Data/hora atual no fuso America/Sao_Paulo."""
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


def _time_header() -> str:
    """Bloco com data/hora atual para injetar no system_instruction.

    Colocado no FINAL do prompt (após o PROMPT_CORE e catálogos) para que
    o cache implícito do Gemini continue valendo sobre o prefixo estável.
    """
    now = _current_time_sp()
    return (
        "\n\n---\n\n## ⏰ DATA/HORA ATUAL (America/Sao_Paulo)\n"
        f"**Agora:** {now.strftime('%d/%m/%Y %H:%M')} ({_DAYS_PT[now.weekday()]})\n\n"
        "Use esta referência OBRIGATORIAMENTE para:\n"
        "- Escolher a saudação: 'bom dia' (05:00–11:59), 'boa tarde' (12:00–17:59), "
        "'boa noite' (18:00–23:59 e madrugada).\n"
        "- Calcular datas relativas (hoje, amanhã, próxima sexta, etc.).\n"
    )

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# ---------------- retry on overload ----------------

_OVERLOAD_HINTS = (
    "overloaded",
    "model is overloaded",
    "try again later",
    "resource exhausted",
    "unavailable",
    "service unavailable",
)


def _is_overload(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (429, 500, 502, 503, 504):
            return True
    msg = str(exc).lower()
    return any(h in msg for h in _OVERLOAD_HINTS)


def _is_non_retriable(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ClientError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code in (400, 401, 403, 404):
            return True
    return False


async def call_with_retry(fn: Callable[[], Any], *, max_tries: int = 6, base: float = 4.0,
                          max_wait: float = 60.0, label: str = "gemini") -> Any:
    """Executa fn() com backoff exponencial + jitter quando detectar sobrecarga."""
    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return await asyncio.to_thread(fn) if not asyncio.iscoroutinefunction(fn) else await fn()
        except Exception as e:
            last_exc = e
            if _is_non_retriable(e):
                logger.error("[%s] Erro nao-retriavel: %s", label, e)
                raise
            if not _is_overload(e):
                logger.error("[%s] Erro inesperado: %s", label, e)
                raise
            wait = min(max_wait, base * (2 ** (attempt - 1)))
            jitter = wait * random.uniform(-0.25, 0.25)
            wait = max(1.0, wait + jitter)
            logger.warning("[%s] Sobrecarga detectada (attempt=%d/%d, wait=%.1fs, error=%s)",
                           label, attempt, max_tries, wait, e)
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ---------------- conversão de history para types do google-genai ----------------

def _history_to_contents(history: list[dict]) -> list[gtypes.Content]:
    contents: list[gtypes.Content] = []
    for h in history:
        role = h.get("role")
        text = (h.get("parts") or [{}])[0].get("text", "")
        if not text:
            continue
        contents.append(gtypes.Content(role=role, parts=[gtypes.Part.from_text(text=text)]))
    return contents


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    meta = getattr(response, "usage_metadata", None)
    if not meta:
        return (0, 0, 0)
    inp = getattr(meta, "prompt_token_count", 0) or 0
    out = getattr(meta, "candidates_token_count", 0) or 0
    total = getattr(meta, "total_token_count", 0) or (inp + out)
    return (inp, out, total)


# ---------------- fallbacks para resposta vazia ----------------

def _simplified_system_prompt() -> str:
    """Camada 2: system prompt curto MAS com acesso às tools."""
    now = _current_time_sp()
    return (
        "Você é a Zoe, assistente da Academia Seven (Seven Fitness) no WhatsApp. "
        "Jovem, simpática e objetiva. Responda em português, de forma breve e natural.\n\n"
        f"Data/Hora atual (America/Sao_Paulo): {now.strftime('%d/%m/%Y %H:%M')} "
        f"({_DAYS_PT[now.weekday()]}).\n"
        "Saudação: 'bom dia' (05–11), 'boa tarde' (12–17), 'boa noite' (18–23).\n\n"
        "Ferramentas disponíveis — USE QUANDO FIZER SENTIDO:\n"
        "- lista_horarios(modalidade, data): consulta vagas para aula experimental.\n"
        "- agenda_aula(modalidade, data, hora, nome_completo): confirma o agendamento.\n"
        "- salva_nome(nome): salva o nome do lead.\n"
        "- classifica_contato(tipo): 'lead' ou 'aluno'.\n"
        "- atendimento_humano(motivo): transfere para a recepção quando necessário.\n\n"
        "Regras críticas (OBEDEÇA SEM EXCEÇÃO):\n"
        "- Se o lead pediu agendamento/horários, CHAME lista_horarios antes de responder.\n"
        "- NUNCA mande o lead falar em outro número — você já está no WhatsApp da academia.\n"
        "- **PROIBIDO** mencionar 'problema técnico', 'probleminha', 'instabilidade', "
        "'sistema fora', 'erro', 'falha' ou qualquer palavra que indique falha técnica, "
        "MESMO QUE a ferramenta tenha retornado erro. Se a tool falhar, use a tool "
        "atendimento_humano e diga apenas: 'Deixa eu chamar nossa recepção pra te ajudar "
        "com os horários, tá bom? 😉'\n"
        "- Academia não atende aula experimental aos domingos."
    )


async def _fallback_with_tools(
    client: "genai.Client", contents: list, phone: str, max_iters: int = 2
) -> str:
    """Camada 2: prompt simplificado + tools habilitadas (mini-loop próprio)."""
    config = gtypes.GenerateContentConfig(
        system_instruction=_simplified_system_prompt(),
        tools=[ALL_TOOLS],
        temperature=0.6,
    )

    local_contents = list(contents)  # cópia — não polui o chat_with_tools principal

    for it in range(max_iters):
        def _call():
            return client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=local_contents,
                config=config,
            )

        try:
            response = await call_with_retry(
                _call, label=f"gemini.fallback[{phone}]", max_tries=3
            )
        except Exception as e:
            logger.warning("[%s] fallback com tools falhou: %s", phone, e)
            return ""

        candidate = (response.candidates or [None])[0]
        if candidate is None or candidate.content is None:
            return ""

        function_calls: list[gtypes.FunctionCall] = []
        text_parts: list[str] = []
        for part in (candidate.content.parts or []):
            if getattr(part, "function_call", None):
                function_calls.append(part.function_call)
            elif getattr(part, "text", None):
                text_parts.append(part.text)

        logger.info(
            "[%s] fallback iter=%d fc=%d txt=%d", phone, it,
            len(function_calls), len(text_parts),
        )

        if not function_calls:
            text = "\n".join(text_parts).strip()
            logger.info("[%s] fallback com tools produziu %d chars", phone, len(text))
            return text

        local_contents.append(candidate.content)
        response_parts: list[gtypes.Part] = []
        for fc in function_calls:
            args = dict(fc.args or {})
            result = await dispatch(fc.name, phone, args)
            logger.info("[%s] fallback tool %s(%s) -> %s",
                        phone, fc.name, args, str(result)[:200])
            response_parts.append(
                gtypes.Part.from_function_response(name=fc.name, response=result)
            )
        local_contents.append(gtypes.Content(role="user", parts=response_parts))

    logger.warning("[%s] fallback com tools esgotou %d iterações", phone, max_iters)
    return ""


_NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ]{2,20}(?:\s+[A-Za-zÀ-ÿ]{2,20})?$")


def _hardcoded_fallback(user_message: str) -> str:
    """Camada 3: resposta natural (sem revelar falha técnica)."""
    msg = (user_message or "").strip()
    if _NAME_RE.match(msg):
        nome = msg.split()[0].capitalize()
        return (
            f"[FINALIZADO=0] Prazer, {nome}! 😃\n\n"
            "Me conta, o que você gostaria de saber sobre a Academia Seven?"
        )
    return (
        "[FINALIZADO=0] Oi! 😊\n\n"
        "Me conta um pouco mais sobre o que você está buscando? "
        "Posso te ajudar com informações sobre modalidades, valores e horários!"
    )


# ---------------- chat principal com tools ----------------

async def chat_with_tools(phone: str, user_message: str, lead_name: str = "",
                          max_tool_iters: int = 5) -> tuple[str, tuple[int, int, int]]:
    client = _get_client()

    history = await get_chat_history(phone)
    contents = _history_to_contents(history)
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=user_message)]))

    # Prefixo (PROMPT_CORE + catálogos) fica cacheado implicitamente pelo Gemini;
    # o _time_header() vai no final para não invalidar o cache do prefixo.
    system_instruction = build_system_prompt(user_message) + _time_header()
    config = gtypes.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[ALL_TOOLS],
        temperature=0.4,
    )

    await append_chat_history(phone, "user", user_message)

    final_text = ""
    tokens_acc = [0, 0, 0]
    pending_text: list[str] = []
    empty_retries = 0

    for it in range(max_tool_iters):
        def _call():
            return client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=contents,
                config=config,
            )

        response = await call_with_retry(_call, label=f"gemini.chat[{phone}]")

        inp, out, tot = _usage_tokens(response)
        tokens_acc[0] += inp
        tokens_acc[1] += out
        tokens_acc[2] += tot

        candidate = (response.candidates or [None])[0]
        if candidate is None:
            logger.warning("[%s] iter=%d candidate=None", phone, it)
            break

        finish = getattr(candidate, "finish_reason", None)
        content = candidate.content
        if content is None:
            logger.warning("[%s] iter=%d content=None finish=%s", phone, it, finish)
            break

        function_calls: list[gtypes.FunctionCall] = []
        text_parts: list[str] = []
        for part in (content.parts or []):
            if getattr(part, "function_call", None):
                function_calls.append(part.function_call)
            elif getattr(part, "text", None):
                text_parts.append(part.text)

        logger.info("[%s] iter=%d fc=%d txt=%d finish=%s txt_preview=%s",
                     phone, it, len(function_calls), len(text_parts), finish,
                     (text_parts[0][:120] if text_parts else ""))

        if not function_calls:
            final_text = "\n".join(text_parts).strip()
            if final_text:
                contents.append(content)
                break
            # Resposta vazia (sem text e sem tool call) — Gemini thinking pode
            # retornar apenas partes de raciocínio sem output real. Retry.
            if not pending_text and empty_retries < 2:
                empty_retries += 1
                logger.warning("[%s] iter=%d resposta vazia (retry %d/2), reenviando",
                               phone, it, empty_retries)
                continue
            break

        if text_parts:
            pending_text.extend(text_parts)

        contents.append(content)

        response_parts: list[gtypes.Part] = []
        for fc in function_calls:
            args = dict(fc.args or {})
            result = await dispatch(fc.name, phone, args)
            logger.info("[%s] tool %s(%s) -> %s", phone, fc.name, args, str(result)[:200])
            response_parts.append(
                gtypes.Part.from_function_response(name=fc.name, response=result)
            )

        contents.append(gtypes.Content(role="user", parts=response_parts))

    if not final_text and pending_text:
        final_text = "\n".join(pending_text).strip()
        logger.info("[%s] using pending_text as final (%d chars)", phone, len(final_text))

    # Camada 2: prompt simplificado com tools habilitadas (mini-loop próprio)
    if not final_text:
        logger.warning("[%s] todas iterações vazias — tentando fallback simplificado", phone)
        final_text = await _fallback_with_tools(client, contents, phone)

    # Camada 3: fallback hardcoded (garantia contratual: nunca retorna vazio)
    if not final_text:
        logger.error("[%s] fallback com tools também vazio — usando hardcoded", phone)
        final_text = _hardcoded_fallback(user_message)

    if final_text:
        await append_chat_history(phone, "model", final_text)

    return final_text, (tokens_acc[0], tokens_acc[1], tokens_acc[2])


# ---------------- transcrição de áudio ----------------

async def transcribe_audio(audio_bytes: bytes) -> str:
    client = _get_client()

    def _call():
        return client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                gtypes.Content(
                    role="user",
                    parts=[
                        gtypes.Part.from_text(
                            text="Transcreva esse áudio fielmente. Retorne APENAS o texto transcrito, sem comentários."
                        ),
                        gtypes.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                    ],
                )
            ],
        )

    response = await call_with_retry(_call, label="gemini.transcribe")
    return (response.text or "").strip()


# ---------------- análise de imagem ----------------

async def analyze_image(image_bytes: bytes) -> str:
    client = _get_client()

    def _call():
        return client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[
                gtypes.Content(
                    role="user",
                    parts=[
                        gtypes.Part.from_text(text="Descreva esta imagem em até 50 palavras, em português."),
                        gtypes.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    ],
                )
            ],
        )

    response = await call_with_retry(_call, label="gemini.image")
    return (response.text or "").strip()


# ---------------- resumo pós-conversa ----------------

async def generate_summary(phone: str) -> str:
    history = await get_chat_history(phone)
    if not history:
        return ""

    lines = []
    for entry in history[-10:]:
        role = "Atendente" if entry.get("role") == "model" else "Lead"
        text = (entry.get("parts") or [{}])[0].get("text", "")
        if text:
            lines.append(f"{role}: {text[:200]}")
    if not lines:
        return ""

    client = _get_client()
    prompt = (
        "Com base no trecho de conversa de uma academia de ginástica, escreva um resumo de 1 a 2 frases "
        "em português sobre quem é esse lead e qual o interesse dele. Seja objetivo.\n\n"
        + "\n".join(lines)
    )

    def _call():
        return client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
        )

    try:
        response = await call_with_retry(_call, label="gemini.summary", max_tries=3)
        return (response.text or "").strip()
    except Exception:
        return ""


# ---------------- mensagem de reativação (scheduler) ----------------

REACTIVATION_PROMPT = """### IDENTIDADE E OBJETIVO
Você é a Zoe, assistente de IA carismática e acolhedora da Academia Seven.
Sua missão é reativar um contato (lead) que parou de responder.

### ENTRADAS
Estágio do Follow-up: {stage}
Data/Hora Atual (America/Sao_Paulo): {now}
Nome do Lead: {nome}

### LÓGICA DE ESTÁGIOS (24h entre cada)
- Estágio 1: leve e contextual. Ex: "Oi [Nome], [bom dia/boa tarde]! Passando só para saber se conseguiu dar uma olhadinha no que te mandei ontem. 🥰"
- Estágio 2: empatia com a rotina. Ex: "Oi [Nome]! Imagino que ontem foi corrido. Quando tiver um tempinho, me avisa se podemos retomar?"
- Estágio 3: retomada final e acolhedora. Ex: "Oi [Nome], vou encerrar nosso atendimento por aqui, mas sigo à disposição quando quiser treinar! 💪"

### REGRAS DE OURO
1. NÃO REPITA frases já enviadas no histórico.
2. SAUDAÇÃO pela hora atual: "bom dia" (05-11), "boa tarde" (12-17), "boa noite" (18-23).
3. Máximo 25 palavras. Seja concisa.
4. Use o nome 1x (ou inicie com "Oi!" se não tiver). Nunca use "Lead".
5. NÃO pergunte "como posso ajudar". Convide para retomar a conversa.
6. Retorne APENAS a mensagem. Texto puro, sem colchetes, prefixos ou tags.
7. Se não conseguir gerar, retorne vazio.
"""


async def generate_reactivation_message(phone: str, nome: str, stage: int, now_str: str) -> str:
    history = await get_chat_history(phone)

    client = _get_client()
    system = REACTIVATION_PROMPT.format(stage=stage, now=now_str, nome=nome or "")

    contents = _history_to_contents(history)
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=f"Gere o follow-up de estágio {stage}.")]))

    config = gtypes.GenerateContentConfig(
        system_instruction=system,
        temperature=0.6,
    )

    def _call():
        return client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=contents,
            config=config,
        )

    try:
        response = await call_with_retry(_call, label=f"gemini.reactivation[{phone}]", max_tries=4)
    except Exception:
        return ""

    text = (response.text or "").strip()
    # Limpa eventuais tags vazadas
    import re as _re
    text = _re.sub(r"\[FINALIZADO=\d\]\s*", "", text).strip()
    return text
