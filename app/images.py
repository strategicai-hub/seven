"""
Dicionário de mídias que a IA pode referenciar via tags [IMAGEM_X].
Imagens hospedadas na própria VPS, servidas de app/media via StaticFiles
(montado em /{CLIENT_SLUG}/media). As URLs são montadas a partir de
settings.MEDIA_BASE_URL. NÃO usar Wix nem nenhum host externo.
"""

from app.config import settings

_BASE = settings.MEDIA_BASE_URL.rstrip("/")


def _media(filename: str) -> str:
    return f"{_BASE}/media/{filename}"


MEDIA_DICT: dict[str, dict] = {
    "[IMAGEM_PLANOS_VALORES]": {
        "url": _media("planos_valores.jpeg"),
        "type": "image",
    },
    "[IMAGEM_HORARIO]": {
        "url": _media("horario.jpeg"),
        "type": "image",
    },
    "[IMAGEM_FITDANCE]": {
        "url": _media("fitdance.jpeg"),
        "type": "image",
    },
    "[IMAGEM_CROSS]": {
        "url": _media("cross.jpeg"),
        "type": "image",
    },
    "[IMAGEM_MUAYTHAI]": {
        "url": _media("muaythai.png"),
        "type": "image",
    },
    "[IMAGEM_COMPLETO]": {
        "url": _media("completo.png"),
        "type": "image",
    },
    "[IMAGEM_COLETIVAS]": {
        "url": _media("coletivas.png"),
        "type": "image",
    },
    "[IMAGEM_ANIVERSARIO]": {
        "url": _media("aniversario.jpeg"),
        "type": "image",
    },
}
