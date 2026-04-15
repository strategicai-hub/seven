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
from typing import Any, Callable, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as gtypes

from app.config import settings
from app.prompt import build_system_prompt
from app.services.redis_service import append_chat_history, get_chat_history
from app.tools import ALL_TOOLS, dispatch

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


# ---------------- chat principal com tools ----------------

async def chat_with_tools(phone: str, user_message: str, lead_name: str = "",
                          max_tool_iters: int = 5) -> tuple[str, tuple[int, int, int]]:
    client = _get_client()

    history = await get_chat_history(phone)
    contents = _history_to_contents(history)
    contents.append(gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=user_message)]))

    system_instruction = build_system_prompt(user_message)
    config = gtypes.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[ALL_TOOLS],
        temperature=0.4,
    )

    await append_chat_history(phone, "user", user_message)

    final_text = ""
    tokens_acc = [0, 0, 0]

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
            break

        content = candidate.content
        if content is None:
            break

        function_calls: list[gtypes.FunctionCall] = []
        text_parts: list[str] = []
        for part in (content.parts or []):
            if getattr(part, "function_call", None):
                function_calls.append(part.function_call)
            elif getattr(part, "text", None):
                text_parts.append(part.text)

        # Se não há function call, é resposta final
        if not function_calls:
            final_text = "\n".join(text_parts).strip()
            if final_text:
                contents.append(content)
            break

        # Append o turno do modelo (inclui as function_calls) ao contents
        contents.append(content)

        # Executa cada tool e adiciona function_response
        response_parts: list[gtypes.Part] = []
        for fc in function_calls:
            args = dict(fc.args or {})
            result = await dispatch(fc.name, phone, args)
            logger.info("[%s] tool %s(%s) -> %s", phone, fc.name, args, str(result)[:200])
            response_parts.append(
                gtypes.Part.from_function_response(name=fc.name, response=result)
            )

        contents.append(gtypes.Content(role="tool", parts=response_parts))
        # loop continua e o modelo reage ao function_response

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
