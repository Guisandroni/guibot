# Guibot — bot de chat Kick

Bot em Python para [Kick](https://kick.com), usando [KickForge](https://github.com/kickforge-dev/kickforge) (`kickforge_core`). Reage ao chat em tempo real (WebSocket/Pusher ou webhook), envia mensagens pela API, pode usar um assistente LLM e mantém estatísticas de atividade para sorteios e rankings.

## Requisitos

- Python 3.10+
- Conta de desenvolvedor Kick (`KICK_CLIENT_ID` / `KICK_CLIENT_SECRET`)
- Autenticação KickForge no disco (`kickforge auth`) para token OAuth alinhado ao canal — ver [.env.example](.env.example)

```bash
pip install -r requirements.txt
```

Copia `.env.example` para `.env` e preenche credenciais e canal (`KICK_CHANNEL` ou lista em `kick.channels` no `config.yaml`).

### Canal onde o bot interage

No **`.env`** (`KICK_CHANNEL` ou `KICK_CHANNELS`) e no **`config.yaml`** (`kick.channel` ou `kick.channels`) defines o **slug do canal Kick** em que o bot vai ouvir o chat e enviar mensagens. É sempre o mesmo conceito: o stream/canal com o qual queres que o bot interaja.  
Se usares variáveis de ambiente, elas **sobrescrevem** os valores do YAML (prioridade: `KICK_CHANNELS` → lista no YAML → `KICK_CHANNEL` → `kick.channel`). Para vários canais num só processo, usa CSV em `KICK_CHANNELS` ou lista em `kick.channels` (com `KICK_MODE=websocket`).

### Autenticação KickForge (antes do primeiro `python bot.py`)

Depois de colocares no `.env` as chaves de **developer** da Kick (`KICK_CLIENT_ID` e `KICK_CLIENT_SECRET`), **tens de autenticar** antes de executar o bot: isso grava tokens em `~/.kickforge/tokens.json` alinhados ao canal.

No diretório do projeto (para carregar o `.env` correctamente), corre o script:

```bash
python scripts/kickforge_auth.py --channel <slug_do_canal>
```

O script invoca o **`kickforge auth`** do KickForge: **abre o navegador** para iniciares sessão na **conta Kick** com a qual queres que o bot opere (por exemplo a conta que pode escrever no chat ou a que corresponde ao teu `chat_poster_type`). Sem este passo, o bot pode falhar a ligar ao chat ou ao enviar mensagens (404, token desactualizado, etc.).

Se mudares de canal ou de credenciais OAuth, volta a correr o comando com o `--channel` correcto.

## Arranque

O bot carrega sempre `.env` e `config.yaml` **na pasta onde está `bot.py`**, independentemente do diretório de trabalho atual.

```bash
python bot.py
```

Sem canal configurado (`KICK_CHANNEL`, `KICK_CHANNELS` ou `kick.channel` / `kick.channels`) o processo termina com erro.

### Modos (`kick.mode` / `KICK_MODE`)

| Modo        | Uso |
|------------|-----|
| `websocket`| Liga ao chat via Pusher (recomendado para um ou vários canais) |
| `webhook` / `hybrid` | Precisa de URL pública para webhooks Kick |

**Multi-canal:** define `KICK_CHANNELS=slug1,slug2` (CSV). Só é suportado com `KICK_MODE=websocket`; um processo por canal se precisares de webhook noutro cenário.

### Envio no chat (`chat_poster_type`)

- **`user`** — mensagens como utilizador OAuth no canal do streamer (precisa de permissões e `kickforge auth` correto).
- **`bot`** — envio como conta bot Kick (`KICK_CHAT_POSTER_TYPE=bot` ou `kick.chat_poster_type`).

Detalhes e 404 no POST `/chat`: ver comentários em [.env.example](.env.example).

---

## Configuração (`config.yaml`)

### Kick (`kick`)

- **`channel`** / **`channels`** — slug(s) do canal **onde o bot interage** (equivalente a `KICK_CHANNEL` / `KICK_CHANNELS` no `.env`; ver secção *Canal onde o bot interage* mais acima neste README).
- **`mode`** — `websocket`, `webhook` ou `hybrid`.
- **`chat_poster_type`** — `user` ou `bot`.

Variáveis de ambiente sobrescrevem quando indicadas (ex.: `KICK_CHANNEL`, `KICK_CHANNELS`, `KICK_MODE`).

### Webhook (`webhook`)

Host/porta/path do servidor HTTP quando usas modos com webhook (`host`, `port`, `path`).

### Bot (`bot`)

#### Prefixo e comandos fixos

- **`prefix`** — por defeito `!`.
- **`commands`** — comandos estáticos: cada entrada tem `response` (texto) e `cooldown` (segundos).

Exemplo: `!schedule` e `!socials` definidos no YAML respondem com o texto configurado.

#### Moderação (`bot.moderation`)

Opcional: palavras bloqueadas, links, spam repetido, caps, timeouts e apagar mensagens via API Kick. Aplica-se só aos canais listados em `moderation.channels` ou na env `KICK_MODERATION_CHANNELS` (por defeito o primeiro canal configurado).

#### Mensagens periódicas (`timed_messages`)

Lista de entradas com `messages` + `interval` (segundos, escolha aleatória) ou `message` + `interval` (uma frase em ciclo).

#### Comentários aleatórios (`comment_spam`)

Carrega linhas de ficheiros `.txt` num diretório (`directory`) e envia periodicamente se `enabled: true`.

### Agente LLM (`agent`)

Assistente opcional (OpenAI-compatible: NVIDIA NIM, OpenCode Go, OpenAI).

- **`enabled`** — ligar/desligar.
- **`trigger`** — comando para perguntas (ex. `!ana`).
- **`cooldown_seconds`** — anti-spam por utilizador.
- Menções a **`KICK_BOT_USERNAME`** também disparam o agente (no `.env`).

Chaves API: prioridade `NVIDIA_API_KEY` → `OPENCODE_API_KEY` → `OPENAI_API_KEY` (ver [.env.example](.env.example)).

### Estatísticas de chat e sorteio (`bot.chat_activity`)

Guarda eventos de mensagem por canal num ficheiro JSON (debounce, TTL e limite de eventos por canal). Serve para **`!sorteio`**, **`!topchat`** e **`!clear`**.

| Opção | Descrição |
|-------|-----------|
| `enabled` | Liga o sistema de stats e os comandos dinâmicos. |
| `path` | Caminho do JSON (relativo ao projeto ou absoluto), ex. `data/chat_activity.json`. |
| `max_retention_seconds` | Eventos mais velhos são removidos (limpeza). |
| `max_events_per_channel` | Teto de eventos por canal. |
| `debounce_seconds` | Agrupa gravações ao disco para não escrever em cada mensagem. |
| `count_command_messages` | Se `false`, mensagens que começam pelo prefixo (`!`) **não** entram na contagem. |
| `default_sorteio_use_session` | Se `true`, `!sorteio` / `!topchat` **sem** duração usam o período desde o arranque do bot; se `false`, usa `default_sorteio_window_seconds`. |
| `default_sorteio_window_seconds` | Janela em segundos quando `default_sorteio_use_session` é `false`. |
| `sorteio_mods_only` | Só mods/streamer podem usar `!sorteio`. |
| `cooldown_sorteio` / `cooldown_topchat` | Cooldowns em segundos. |
| `topchat_limit` | Quantos lugares mostrar no ranking do `!topchat`. |
| `clear_mods_only` | Se `true`, só mods/streamer podem `!clear`. |
| `cooldown_clear` | Cooldown do `!clear`. |

O ficheiro em `data/` está ignorado no git (ver [.gitignore](.gitignore)).

---

## Comandos no chat

### Comandos YAML (`bot.commands`)

Definidos em `config.yaml`: `!<nome>` → resposta fixa + cooldown.

### Agente (`agent.trigger` + menções)

- `!<trigger> <pergunta>` — envia a pergunta ao LLM e responde no chat.
- Mencionar o bot (`KICK_BOT_USERNAME`) no início da mensagem também funciona.

### Olá / “selam”

Mensagens com `hello` ou `selam` (case-insensitive) podem receber uma resposta de boas-vindas (comportamento fixo no código).

### Estatísticas (`chat_activity` ligado)

| Comando | Quem | O que faz |
|---------|------|-----------|
| `!sorteio` | Todos, ou só mods se `sorteio_mods_only: true` | Entre quem tem **mais mensagens** no período, escolhe **um vencedor ao acaso** (empate justo). Sem argumentos usa sessão ou janela conforme `default_sorteio_*`. |
| `!sorteio <tempo>` | Idem | Janela móvel explícita (ver formatos abaixo). |
| `!topchat` | Todos | Mostra um **ranking** curto e quantos utilizadores únicos há no mesmo período que o sorteio por defeito. |
| `!topchat <tempo>` | Todos | Igual, com janela explícita. |
| `!clear` | Por defeito só **mods/streamer** (`clear_mods_only`) | Apaga **no bot** os eventos guardados **deste canal** (ficheiro JSON). **Não** apaga mensagens visíveis no chat da Kick. Novas mensagens voltam a contar a partir daí. |

#### Formatos de duração (`!sorteio` / `!topchat`)

| Exemplo | Significado |
|---------|-------------|
| `!sorteio 120` | **120 segundos** (número sozinho = sempre segundos) |
| `!sorteio 2min`, `!sorteio 2 min`, `!sorteio 2m` | **2 minutos** |
| `!sorteio 1h` | **1 hora** |
| `!sorteio 90s` | **90 segundos** |

---

## Outros eventos automáticos

O bot pode responder a **novo follow**, **subscrição** e **kicks oferecidos** (mensagens configuradas no código dos handlers).

---

## Estrutura útil do projeto

| Ficheiro | Função |
|----------|--------|
| `bot.py` | Entrada principal, eventos Kick, comandos, moderação, timers |
| `agent.py` | Cliente LLM e prompts do assistente |
| `chat_activity.py` | Persistência e contagens para sorteio/ranking |
| `config.yaml` | Configuração (credenciais sensíveis costumam estar só no `.env`) |
| `scripts/kickforge_auth.py` | Ajuda a correr `kickforge auth` com `.env` carregado |

---

## Notas

- O terminal **não** lista todas as mensagens do chat por defeito; o bot processa-as nos handlers. Para debug, podes subir o nível de logging ou acrescentar logs pontuais.
- Limites de tamanho das mensagens enviadas ao chat estão alinhados com regras da Kick (truncagem em `bot.py`).
