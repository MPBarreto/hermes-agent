---
name: skill-sdr
description: "Regras de domínio centralizadas do SDR Afiro — stages do HubSpot, limites, convenções de ferramenta, resiliência MCP, critérios de email e supressão. Anexada a TODOS os crons SDR para que os prompts fiquem finos e as regras vivam num lugar só."
version: 1.0.0
author: Afiro
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sdr, afiro, policy, hubspot, stages, governance]
---

# SDR Afiro — Regras de Domínio (Policy)

Esta skill concentra TODAS as regras de negócio do SDR Afiro. Os cron jobs SDR
(Lead Discovery, Email Cadence, Reply Detector, Weekly Report) anexam esta skill e
mantêm prompts finos: o prompt declara só o **objetivo do run**; as **regras** (stages,
limites, ferramentas, retries, supressão) estão aqui. Mudar uma regra = editar esta skill,
não os 4 prompts.

> As skills operacionais específicas (sdr-lead-discovery, sdr-hubspot-sync,
> sdr-email-cadence, sdr-reply-detector, sdr-weekly-report) contêm o **como** de cada
> fluxo. Esta skill contém o **o quê é permitido / proibido / convencionado** comum a todos.

## 1. Stages do HubSpot (fonte da verdade — NÃO inverter)

IDs confirmados via `get_properties(objectType='deals', propertyNames=['dealstage'])`.

| dealstage (ID) | Label | Papel no SDR |
|---|---|---|
| `7035178` | Lead | Revisão manual (ex: email ambíguo `needs_review`) |
| `decisionmakerboughtin` | **Mailling Funnel** | Email pessoal confirmado + ICP ≥ 60 → cadência ativa |
| `1386309983` | **Linkedin Funnel** | Sem email pessoal, só LinkedIn URL |
| `534034` | Approaching | Respondeu ao email/DM (destino após reply) |
| `7035179` | Apresentação | Reunião agendada |
| `963304224` | Onboarding | Contrato fechado |
| `closedwon` | Cliente Ativo | Convertido |
| `184068381` | MQL | Qualificado por marketing |
| `1386915876` | ⛔ **Churn** | **NUNCA usar para SDR outbound** |
| `closedlost` | Não Qualificado | Fora do ICP / sem interesse / opt-out |

**Dois erros fáceis (não cometer):**
1. Mailling Funnel é o slug `decisionmakerboughtin`, **não** um ID numérico.
2. `1386915876` é **Churn**, **não** LinkedIn Funnel. Jamais colocar lead SDR nesse stage.

### Regra de posicionamento
- Email pessoal verificado (`email_source` ∈ {apollo, apify, hubspot, verified}) e
  `icp_score ≥ 60` → `decisionmakerboughtin` (Mailling Funnel).
- Email genérico (`oi@`, `contato@`, `marketing@`, `vendas@`, `sac@`, etc.) → tratar como
  sem email → `1386309983` (Linkedin Funnel).
- Sem email pessoal ou só LinkedIn, ICP ≥ 40 → `1386309983` (Linkedin Funnel).
- Email nome-da-marca@domínio (`talbor@talbor.com.br`) → ambíguo → stage `7035178` (Lead),
  `needs_review`, **não entra na cadência**.

> Não restringir Mailling a `email_source == "apollo"` — email do Apify é igualmente válido.

## 2. Status do lead (hs_lead_status) — máquina de estados LinkedIn

| Valor | Significado |
|---|---|
| `NEW` | Conexão não solicitada → disponível p/ convite |
| `OPEN_DEAL` | Convite enviado → aguardando aceite |
| `CONNECTED` | Aceito → disponível p/ mensagem |

## 3. Limites (guardrails de volume)

| Fluxo | Limite |
|---|---|
| Lead Discovery | máx **20 novos leads/run** |
| Email Cadence | máx **50 emails/dia**; **1 cadência por lead** (5 emails/21 dias) |
| LinkedIn (connect) | máx **5 conexões/dia** |
| LinkedIn (message) | máx **5 mensagens/dia**, 1 por lead |

## 4. Convenções de ferramenta (OBRIGATÓRIO em cron)

- **Scripts Python**: usar SEMPRE a tool `terminal`. **NUNCA** `execute_code` — é bloqueado
  em cron (sem usuário para aprovar).
- **Enviar email**: `BODY="..."; SIG=$(cat ~/.hermes/signatures/cold.txt); printf '%s\n%s'
  "$BODY" "$SIG" | python3 -m hermes_cli.main send --to 'email:LEAD' --subject 'ASSUNTO'`.
  Saída `sent` = sucesso. Assinatura cold (texto) SEMPRE anexada; nunca HTML com imagens.
- **Remetente**: marcos@eyby.com.br (canal já configurado).
- **Slack**: entregar resumos no `#afiro-agent`.

## 5. Resiliência MCP

Se Apollo/HubSpot/Apify retornar `not connected` ou `unreachable`: aguardar ~60s e tentar
**uma vez** antes de desistir. Os MCPs podem estar reconectando após refresh de token.

## 6. Critérios de email

- **Pessoal** (qualifica p/ Mailling): nominal — `nome@empresa`.
- **Genérico** (NÃO qualifica): `oi@`, `ola@`, `contato@`, `info@`, `hello@`, `marketing@`,
  `vendas@`, `sales@`, `atendimento@`, `suporte@`, `sac@`, `comercial@`, `admin@`.
- **Ambíguo** (nome-da-marca@domínio): → Lead/`needs_review`.

## 7. Supressão

- Opt-outs e bounces hard → registrar em `~/.hermes/sdr/suppression_list.txt`.
- Nunca enviar para quem está na lista de supressão.
- Reply recebido → parar cadência, mover deal para Approaching (`534034`).

## 8. Enriquecimento (cascata custo-otimizada)

Domínio → decisor (Apollo primeiro; senão Apify actor BARATO) → contato (email/telefone).
**Actors Apify aprovados** (baixo custo): `apt_marble/linkedin-decision-makers-scraper`
($0.0015), `novashieldai/b2b-lead-enrichment` (grátis), `snipercoder/bulk-linkedin-email-finder`
($0.001), `apimaestro/linkedin-profile-detail` ($0.005).
**PROIBIDOS** (caros): `snipercoder/decision-maker-email-finder` ($25),
`snipercoder/bulk-decision-makers-email-finder` ($20), `caprolok/website-email-phone-finder`.
Nada acima de **$0.05/result** sem aprovação. Apify só grava decisor se a empresa dele bater
com o domínio-alvo (evitar falso positivo).

## 9. Entrega e rastreabilidade

- Toda execução envia resumo ao Slack `#afiro-agent`.
- Email cadence registra nota no Deal: `SDR_EMAIL_SENT|step:N|variant:X|sent_at:ISO`.
- Marcar `tipo_de_contato='e-commerce'` em todo contato criado.
