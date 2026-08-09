---
name: sdr-linkedin-cadence
description: "Opera o funil LinkedIn do SDR Afiro com a tool `linkedin`: lê os contatos do stage Linkedin Funnel no HubSpot, envia convites de conexão, verifica aceites, dispara DMs a partir de modelos em ~/.hermes/linkedin/templates/ e grava o novo hs_lead_status de volta no HubSpot. Use quando o pedido for rodar o ciclo LinkedIn, mandar convites, checar quem aceitou ou enviar mensagem para conexões."
version: 1.0.0
author: Afiro
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sdr, afiro, linkedin, outbound, hubspot, cadence]
    related_skills: [skill-sdr, sdr-hubspot-sync]
---

# SDR LinkedIn — Cadência de Conexão e DM

Executa o funil LinkedIn: convite → aceite → mensagem. As regras de negócio
comuns (stages, limites, supressão, convenções de cron) vivem em `skill-sdr`
e **não são repetidas aqui** — esta skill cobre só o *como* deste fluxo.

> **HubSpot é a fonte da verdade.** A tool `linkedin` clica; o HubSpot lembra.
> A tool não fala com o CRM: você lê os contatos, passa as URLs, recebe o
> resultado por perfil e grava o novo estado de volta.

## 1. Máquina de estados

Contatos no stage **Linkedin Funnel** (`dealstage = 1386309983`), campo
`hs_lead_status` — valores confirmados nesta conta:

| Valor | Label no HubSpot | Significa | Próxima ação |
|---|---|---|---|
| `NEW` | Novo | Ainda não convidado | `connect` |
| `OPEN_DEAL` | Conexão Solicitada | Convite enviado, aguardando | `check_accepted` |
| `CONNECTED` | Linkedin Conectado | Aceitou | `message` |

A URL do perfil vem de **`hs_linkedin_url`** no Contact.

## 2. O ciclo diário

Sempre nesta ordem. Cada etapa alimenta a seguinte.

```
1. connect        NEW        → OPEN_DEAL
2. check_accepted OPEN_DEAL  → CONNECTED
3. message        CONNECTED  → (registra o toque)
```

### Antes de começar

```
linkedin(action='status')
```

Devolve a quota restante do dia e a fila `pending_hubspot_sync`. **Se houver
linhas pendentes, sincronize-as primeiro** (§5) — são ações já executadas no
LinkedIn cuja escrita no HubSpot falhou.

Se a sessão puder estar vencida:

```
linkedin(action='login_status')   → valid | expired | blocked
```

`expired` ou `blocked` → pare o ciclo e avise no Slack (§6). Não adianta
insistir.

### Etapa 1 — Convites

Busque contatos do stage `1386309983` com `hs_lead_status = NEW` e
`hs_linkedin_url` preenchida. Pegue **no máximo 5**.

```
linkedin(action='connect', profiles=['https://www.linkedin.com/in/...', ...])
```

Resultado por perfil:

| `result` | O que houve | O que fazer |
|---|---|---|
| `ok` | Convite enviado e confirmado | `hs_lead_status = OPEN_DEAL` |
| `skipped` + `already_requested_today` | Já convidado hoje | Nada |
| `skipped` + `daily_limit_reached` | Bateu o teto de 5 | Deixa para amanhã |
| `skipped` + `connect_button_not_found` | Sem "Conectar" no cartão **nem** no menu "···" | Ver abaixo |
| `failed` | Erro no fluxo | Nada; artefatos ficam salvos |

O convite vai **sempre sem nota** — a quota mensal de notas do LinkedIn é
pequena e não melhora a taxa de aceite.

#### Onde fica o botão "Conectar"

O LinkedIn usa dois layouts, e a tool cobre os dois:

1. **Exposto no cartão** — botão "Conectar" azul ao lado de "Enviar mensagem".
2. **Dentro do menu "···"** — em contas com muitos seguidores o LinkedIn
   promove "Seguir" + "Enviar mensagem" no cartão e **esconde "Conectar" no
   menu de três pontos**. A tool abre o menu e clica lá dentro.

`connect_button_not_found` só aparece quando não há "Conectar" em **nenhum**
dos dois lugares — aí sim é perfil já conectado, já convidado, ou que não
aceita convite. Não insista; trate por outro canal.

> ⚠️ **Nunca conclua "não tem Conectar" só porque o cartão não mostra.**
> Em 2026-08-07 o perfil `/in/tiago-alvisi` foi diagnosticado assim por
> engano — o botão estava no menu "···" o tempo todo.

> ⚠️ **"Enviar mensagem" não significa conexão.** Esses perfis mostram o botão
> sendo 2º grau, e ele abre um composer **InMail pago**. Por isso o
> `check_accepted` confere o badge de grau antes de declarar aceite.

### Etapa 2 — Aceites

Contatos com `hs_lead_status = OPEN_DEAL`. Até 5 por chamada.

```
linkedin(action='check_accepted', profiles=[...])
```

| `result` | Significa | O que fazer |
|---|---|---|
| `accepted` | Conexão confirmada | `hs_lead_status = CONNECTED` |
| `pending` | Ainda não aceitou | Nada; tenta de novo amanhã |
| `ambiguous` | Mais de uma conta bate com a URL | **Não chute** — revisão manual |
| `failed` | Erro | Nada |

Esta ação **não consome quota** — é leitura.

### Etapa 3 — Mensagens

Contatos com `hs_lead_status = CONNECTED` que ainda não receberam DM. Até 5.

```
linkedin(action='message',
         profiles=['https://www.linkedin.com/in/ana', ...],
         template='dm-step1',
         values={'https://www.linkedin.com/in/ana': {'name':'Ana Silva','company':'Acme'}})
```

`values` é por URL de perfil e alimenta os placeholders. Placeholder sem
valor vira string vazia — não quebra o envio, mas **confira antes**: uma
mensagem com "da ." no meio é pior que não enviar.

| `result` | Significa | O que fazer |
|---|---|---|
| `ok` | Enviada e confirmada | Registre o toque no HubSpot |
| `unclear` | **Pode ter sido enviada** | **Não reenvie.** Confira à mão |
| `skipped` + `blocked_non_first_degree` | Não é conexão de 1º grau | Volte para `check_accepted` |
| `skipped` + `not_found_in_connections` | Não achou na lista | Confirme o aceite |
| `failed` | Erro | Artefatos salvos |

> **`unclear` nunca conta como enviada.** A tool só declara sucesso com
> evidência (mensagem na thread ou caixa esvaziada). Sem evidência, o perfil
> entra em `human_review_required`. Reenviar por cima é o erro caro aqui —
> a pessoa recebe a mesma mensagem duas vezes.

## 3. Modelos de mensagem

Ficam em `~/.hermes/linkedin/templates/`, um `.md` por modelo:

```markdown
---
name: dm-step1
stage: message_1
lang: pt-BR
---
Oi {first_name}, tudo bem?

Vi o trabalho da {company} e queria trocar uma ideia sobre {assunto}.
```

- O nome do modelo é o **nome do arquivo sem `.md`** (`dm-step1.md` → `dm-step1`).
- O frontmatter é informativo; só o corpo é enviado.
- Placeholders: qualquer chave passada em `values`. `{first_name}` é derivado
  de `name` automaticamente quando não vier explícito.
- Para listar o que existe, leia o diretório com `read_file`/`terminal`.
- Criar ou editar modelo = escrever o arquivo. Não precisa mexer em código.

Alternativa sem arquivo: passe `text='...'` em vez de `template=`.

## 4. Limites e travas

Aplicados **dentro da tool** — você não precisa contar:

- **5 convites/dia** e **5 mensagens/dia** (`skill-sdr` §3), no fuso
  America/Sao_Paulo.
- **Máximo 3 perfis por chamada.** Lote maior é recusado. Cada chamada tem
  teto de 300s e, com o ritmo humanizado, um perfil custa ~15s de navegação
  e leitura mais um intervalo que gira em torno de 30s. Para os 5/dia, faça
  duas chamadas (3 + 2).
- **Um perfil por ação por dia.** Repetir devolve `already_requested_today`.
- **Um processo por vez.** Chamada concorrente devolve `busy` — o perfil do
  Chrome não suporta dois processos e corromper significa perder a sessão.

## 5. Gravando de volta no HubSpot

Depois de cada ação, atualize os contatos:

```
manage_crm_objects → contacts → hs_lead_status
  connect ok        → OPEN_DEAL
  check_accepted    → CONNECTED   (só os result='accepted')
```

**Depois que a gravação der certo, feche a fila:**

```
linkedin(action='mark_synced', profiles=[<os mesmos perfis>])
```

Sem isso o ledger continua marcando `hubspot:pending` para sempre, o
`status` reporta pendências falsas e o ciclo seguinte regrava contatos que já
estão corretos. Use `ledger_action='message'` quando o que sincronizou foi um
envio de DM.

Se o MCP HubSpot estiver fora (`not connected` / `unreachable`): espere ~60s
e tente **uma vez** (`skill-sdr` §5). Persistindo, siga — a ação já está no
ledger marcada `hubspot:pending` e aparece em `linkedin(action='status')` no
próximo run. **Não há retry automático**: a reconciliação é você lendo essa
fila no começo do ciclo seguinte e chamando `mark_synced` ao final.

## 6. Quando parar

Pare o funil e reporte no Slack `#afiro-agent`:

- `status='blocked'` — captcha ou checkpoint. **Não insista**: clicar mais
  transforma uma checagem leve em restrição de conta. O usuário resolve à mão
  com `linkedin(action='login')`.
- `status='login_required'` ou `login_status='expired'` — sessão caiu.
- `status='busy'` — outro run está com o perfil. Tente mais tarde.
- `status='unavailable'` — falta Playwright ou o Chromium. A mensagem traz o
  comando exato.

## 7. Login (manual, uma vez)

```
linkedin(action='login')        → abre o Chromium e retorna na hora
                                   (não espera; status='awaiting_login')
# o usuário digita senha + 2FA e FECHA a janela
linkedin(action='login_status') → deve dar 'valid'
```

A sessão fica no perfil persistente e dura semanas. **Só o usuário faz login**
— não existe credencial no `.env` e não deve existir: automatizar senha e 2FA
é o caminho mais rápido para um checkpoint.

## 8. Limitações que importam

- **Abre uma janela visível** na máquina do usuário. Não roda em servidor sem
  display, e um cron só funciona com a máquina ligada e destravada.
- **A tool é lenta de propósito.** Ela lê a página, move o mouse, rola a tela
  e digita caractere a caractere antes de agir, com intervalos sorteados de
  uma distribuição assimétrica (mediana ~26s, cauda até 90s, e a cada ~7
  perfis uma pausa longa de 1,5 a 4 minutos). Um lote de 3 leva 1 a 3
  minutos. **Isso não é travamento** — é o que evita o padrão robótico que
  dispara o captcha. Não tente acelerar.
- **Anti-detecção continua limitada.** O ritmo humanizado remove os sinais
  estatísticos óbvios, mas não altera fingerprint de browser nem usa proxy.
  Os limites de 5/dia seguem sendo a proteção principal; não os contorne
  rodando o ciclo várias vezes ao dia.
- **Os seletores são frágeis por natureza.** Mudança de layout do LinkedIn
  aparece como `connect_button_not_found` ou `message_box_not_found` em
  vários perfis seguidos. Vários `failed` iguais na mesma rodada = avise, não
  insista.
- **O LinkedIn usa classes CSS ofuscadas** (`_6e41928b`, `b0b51075`…) que
  mudam sem aviso. Por isso a tool localiza tudo pela **estrutura**: o cartão
  do perfil pelo `aria-label` que traz o nome do dono; os cards da página de
  conexões subindo a partir de cada link `/in/` enquanto o bloco descrever
  uma só pessoa. Se um dia `connect_button_not_found` aparecer em *todos* os
  perfis, ou `check_accepted` ficar lento e sempre usar `profile_page`,
  suspeite dessas âncoras antes dos seletores de texto.
- **O badge `· 1º` não é o grau de conexão.** Comparando o mesmo perfil antes
  e depois do aceite: em 2º grau aparecem *dois* ordinais (`· 1º` **e**
  `• 2º`); em 1º grau só o `· 1º`. O grau real é o que vem depois do `•`.
  Ler o primeiro ordinal da página faz um contato aceito parecer pendente.
- **Falhas deixam artefatos** em `~/.hermes/linkedin/artifacts/` (PNG + HTML).
  Ao diagnosticar, **abra o PNG primeiro** — em 2026-08-07 o print mostrou
  que a página tinha navegado para uma publicação, algo que a mensagem de
  erro (`send_without_note_not_found`) não revelava. E note que o **HTML não
  captura iframes**: a janela de conversa do LinkedIn roda num iframe, então
  o snapshot pode parecer vazio mesmo com a mensagem visível no print.
- **`blocked` pode ser falso positivo.** A detecção olha a URL e o texto do
  *chrome* da página, nunca o feed — mas se aparecer `blocked` com a conta
  visivelmente normal, confira o print antes de parar o funil. Em 2026-08-07
  um post que dizia "ready to tackle challenges" derrubou uma rodada inteira.
