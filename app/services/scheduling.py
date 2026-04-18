"""Utilitários de distribuição temporal de envios em lote.

Motivação: jobs de follow-up (plan_expiry, birthday, post_trial, absent) são
disparados uma vez por dia e historicamente enviavam todas as mensagens em
sequência no mesmo minuto. Para parecer mais humano e diluir a carga na
Uazapi/WhatsApp, distribuímos cada envio em um horário aleatório dentro de
uma janela configurável (default: 1h).
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def distribute_over_window(
    items: Sequence[T],
    send_fn: Callable[[T], Awaitable[None]],
    window_seconds: int = 3600,
    label: str = "envios",
) -> None:
    """Executa `send_fn(item)` para cada item, espalhando os envios dentro da janela.

    Cada item recebe um offset aleatório uniforme em [0, window_seconds). Os
    envios são ordenados pelo offset crescente e disparados sequencialmente —
    cada um aguarda a diferença entre seu offset e o tempo já decorrido.

    Se `send_fn` lança exceção, o erro é logado e o loop continua nos demais.
    """
    if not items:
        return

    offsets = sorted(
        (random.uniform(0, window_seconds), idx, item)
        for idx, item in enumerate(items)
    )
    logger.info(
        "%s: distribuindo %d envio(s) em janela de %ds",
        label, len(offsets), window_seconds,
    )

    start = asyncio.get_event_loop().time()
    for offset, idx, item in offsets:
        elapsed = asyncio.get_event_loop().time() - start
        wait = max(0.0, offset - elapsed)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await send_fn(item)
        except Exception:
            logger.exception("%s: falha enviando item #%d", label, idx)
