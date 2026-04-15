# Seven Academia — Instruções do projeto

Assistente WhatsApp **Zoe** (Academia Seven / Seven Fitness), migrado do fluxo n8n original (`seven.json` + `Follow UP seven.json`) para serviço Python.

## Regra obrigatória: commit, push e deploy

**Antes de qualquer operação de commit, push ou redeploy, SEMPRE perguntar:**

> "Quer que eu faça commit, push e redeploy agora?"

Aguardar confirmação explícita. Nunca executar automaticamente.

Isso inclui:
- `git commit`
- `git push`
- Redeploy via Portainer (force-update do serviço Swarm)
- Build de imagem Docker com `nocache=true`

## Stack

| Campo | Valor |
|---|---|
| **Repo** | https://github.com/gustavocastilho-hub/seven |
| **Webhook** | https://webhook-whatsapp.strategicai.com.br/seven |
| **Portainer URL** | `https://91.98.64.92:9443` |
| **Endpoint ID** | `1` |
| **Stack** | `seven` |
| **Image tag** | `ghcr.io/gustavocastilho-hub/seven:latest` |
| **Services** | `seven_seven-api`, `seven_seven-worker`, `seven_seven-scheduler` |
| **Credenciais** | `clientes/seven/.env` |
| **RabbitMQ queue** | `seven` |
| **Volume SQLite** | `seven_sqlite` montado em `/data` |

## Arquitetura

Três processos, mesma imagem Docker:

- **seven-api** (`uvicorn app.main:app`): recebe webhook Uazapi em `/seven` → publica na fila RabbitMQ `seven`.
- **seven-worker** (`python worker.py`): consome a fila, aplica debounce 30s em Redis, chama Gemini 2.5 Flash com function calling (tools: `salva_nome`, `classifica_contato`, `lista_horarios`, `agenda_aula`, `atendimento_humano`), envia respostas via Uazapi e persiste estado do lead em SQLite.
- **seven-scheduler** (`python scheduler.py`): APScheduler com 4 jobs:
  - `reactivation` — a cada 1 min: reativa leads com Gemini (stages 1/2/3).
  - `plan_expiry` — 09:00 diário: lembretes 7d/15d via CloudGym v2.
  - `birthday` — 09:07 diário: imagem de aniversário.
  - `post_trial` — 09:04 diário: follow-up D+1 para quem teve aula experimental ontem.

## Integrações externas

- **CloudGym** (unit `2751`):
  - v1 (`api.prod.cloudgym.io`) com Basic Auth → Bearer — usado para `config/classes`, `admin/classattendancelist`, `v1/classattendance`, `customer`.
  - v2 (`api.cloudgym.io`) com username/password → token — usado para `v1/member` (listagem de alunos para jobs de follow-up).
  - Tokens cacheados em Redis (`cg:v1:token`, `cg:v2:token`).
- **Uazapi** (`strategicai.uazapi.com`): envio de texto, mídia (imagens), download de áudio/imagem recebidos.
- **Gemini 2.5 Flash** (`google-genai` SDK): chat com tools + cache implícito + transcrição de áudio + análise de imagem.
- **Google Sheets**: CRM pós-conversa (colunas Data/Hora, WhatsApp, Nome, Resumo).
- **Redis** (`91.98.64.92:6380`): buffer de mensagens (debounce), histórico de chat (últimas 30 mensagens), flags de bloqueio/alerta/dedup.
- **RabbitMQ** (`91.98.64.92:5672`): fila `seven`.

## Economia de tokens (Gemini 2.5 Flash)

1. **Cache implícito**: `system_instruction` é o primeiro bloco de cada chamada. A partir da 2ª mensagem da mesma sessão (em ≤5 min) o Gemini desconta 75% dos tokens cacheados.
2. **Prompt modular** (`app/prompt.py`): `PROMPT_CORE` sempre enviado + `CATALOGO_HORARIOS` e `CATALOGO_PRECOS` só quando a mensagem do lead casa com regex de intenção. Economia média 40-60% por turno.
3. **Function calling nativo**: as 5 tools são passadas como `FunctionDeclaration` (`app/tools.py`); o prompt **não** lista as tools inline.
4. **History** truncado para 30 mensagens (`app/services/redis_service.py`).
5. **Debounce 30s**: várias mensagens do usuário viram 1 chamada.
6. **Modo mudo**: após `atendimento_humano`, o worker nem chama o Gemini — responde `[FINALIZADO=1]` localmente.

## Retry em caso de sobrecarga do Gemini

`app/services/gemini.py::call_with_retry` detecta:
- `google.genai.errors.ServerError` com códigos 429, 500, 502, 503, 504
- mensagens contendo `"overloaded"`, `"unavailable"`, `"try again later"`, `"resource exhausted"`

Backoff exponencial com jitter ±25%, base 4s, teto 60s, **máx 6 tentativas** (~3 min total). Erros 400/401/403/404 sobem imediatamente sem retry.

- No **worker**: se esgotar retries, envia alerta para `ALERT_PHONE` e ativa `modo_mudo` do lead.
- No **scheduler** (reativação): se falhar, pula o lead e tenta no próximo tick.

## Deploy

O processo de redeploy deste projeto é sempre:

1. Criar tarball: `tar -czf /tmp/build-context.tar.gz --exclude='.git' --exclude='node_modules' --exclude='.env' .`
2. Build via Portainer API (endpoint `1`, tag `ghcr.io/gustavocastilho-hub/seven:latest`)
3. Force-update dos 3 serviços Swarm incrementando `TaskTemplate.ForceUpdate`
4. Verificar HTTP 200 em `https://webhook-whatsapp.strategicai.com.br/seven/health`
5. Verificar containers rodando via `docker service ps seven_seven-api` etc.
   - Se algum estado ≠ `running`, ler logs com `docker service logs seven_seven-worker --tail 50` e corrigir antes de encerrar.

Credenciais em `.env` na raiz do projeto (nunca commitado).

Alternativa: push para `main` → GitHub Actions (`.github/workflows/docker-publish.yml`) publica a imagem no GHCR e aciona webhook do Portainer para redeploy automático.

## Variáveis sensíveis

Todas em `.env` (ver `.env.example` para estrutura):
- `GEMINI_API_KEY`
- `UAZAPI_TOKEN`
- `CLOUDGYM_V1_BASIC` (base64 de `dchosen86@gmail.com:170786`)
- `CLOUDGYM_V2_USERNAME`, `CLOUDGYM_V2_PASSWORD`
- `GOOGLE_CREDENTIALS_JSON`, `GOOGLE_SHEET_ID`
- `REDIS_PASSWORD`, `RABBITMQ_PASS`

## Dry-run dos follow-ups

Setar `FOLLOWUP_DRY_RUN=1` faz os 4 jobs apenas logarem o que seria enviado, sem chamar Uazapi. Recomendado no primeiro deploy para validar a listagem da CloudGym v2 antes de ativar o envio real.

## Tom e idioma

- Responder sempre em português brasileiro.
- Respostas curtas e diretas.
- Não usar emojis a menos que solicitado.

## Arquivos de referência (não committar)

- `seven.json` — fluxo n8n original do bot
- `Follow UP seven.json` — fluxo n8n de follow-up original
- `RabbitMQ - seven.json` — fluxo n8n de ingestão webhook → fila
