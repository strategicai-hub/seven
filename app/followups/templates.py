"""
Templates fixos dos follow-ups (extraídos verbatim do fluxo n8n).
Placeholder {primeiroNome} é substituído em tempo de envio.
"""

PLAN_EXPIRY_7D_MENSAL = """Olá {primeiroNome}! Tudo bem?

Seu plano mensal na *Seven Fitness* vence em *7 dias*. Se atente para renovar dentro do prazo e continuar treinando sem interrupções.

Aproveitamos para te convidar a migrar para o *plano semestral*, que oferece mais vantagens e descontos na renovação:

- 15 a 8 dias antes do vencimento: 10% de desconto
- Até 1 dia antes do vencimento: 5% de desconto
(Descontos não cumulativos com outras promoções ou convênios).

Quer saber mais ou já fazer sua renovação? Fale com a gente na recepção!"""


PLAN_EXPIRY_15D_SEMESTRAL = """Olá {primeiroNome}! Tudo bom?

Passando para avisar que o seu plano na Seven Fitness vence em 15 dias.

Se atente para realizar sua renovação dentro do prazo e continuar treinando normalmente, sem interrupções.

E para quem possui o *PLANO SEMESTRAL* e decida renovar no prazo de 15 a 8 dias antes do vencimento, terá 10% de desconto! Renovando até 1 dia antes do vencimento ganha 5% de desconto! (descontos não cumulativos com outras promoções ou convênios)

Não perca tempo e garanta já sua renovação - é só falar com a gente na recepção!"""


PLAN_EXPIRY_FALLBACK = """Olá {primeiroNome}! Tudo bom?

Passando para lembrar que o seu plano na Seven Fitness está próximo do vencimento.

Não perca tempo e garanta já sua renovação - é só falar com a gente na recepção!"""


POST_TRIAL_DAY_AFTER = """Foi um prazer ter você realizando uma aula experimental aqui na Seven Fitness! Esperamos que tenha curtido a energia da turma e sido uma ótima experiência.

Se tiver qualquer dúvida sobre nossos planos e horários, estou à disposição para te ajudar. Será muito legal ter você treinando com a gente!

Quando quiser, é só nos chamar para garantir sua vaga nas próximas aulas."""


def primeiro_nome(nome: str | None) -> str:
    if not nome:
        return "tudo bem"
    return nome.strip().split(" ")[0] or "tudo bem"
