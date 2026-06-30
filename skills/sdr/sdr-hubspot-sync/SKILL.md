---
name: sdr-hubspot-sync
description: "Registra leads qualificados no HubSpot CRM com deduplicação. Cria Company, Contact e Deal posicionando no stage correto do pipeline (Mailling Funnel ou LinkedIn Funnel)."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sdr, hubspot, crm, deduplication, pipeline, deals]
---

# SDR HubSpot Sync

Recebe leads qualificados pelo `sdr-lead-discovery` e os registra no HubSpot com deduplicação completa. Cria os objetos na ordem correta: Company → Contact → Deal, respeitando as regras de posicionamento no pipeline.

## Pipeline de Vendas — Stages

> **IDs confirmados via `get_properties(objectType='deals', propertyNames=['dealstage'])`. Esta é a fonte da verdade — não inverter.**

| Stage ID (dealstage) | Label | Quem entra |
|---|---|---|
| `7035178` | Lead | — |
| `decisionmakerboughtin` | **Mailling Funnel** | Leads com email pessoal confirmado (ICP ≥ 60) |
| `1386309983` | **Linkedin Funnel** | Leads sem email pessoal, apenas LinkedIn URL |
| `534034` | Approaching | Responderam ao email ou DM |
| `7035179` | Apresentação | Reunião agendada |
| `263791505` | Teste 14D | — |
| `963304224` | Onboarding | Contrato fechado, onboarding em curso |
| `closedwon` | Cliente Ativo | Convertido |
| `184068381` | MQL | Leads qualificados por marketing |
| `1386915876` | ⛔ Churn | **NUNCA usar para SDR outbound** — clientes que churnearam |
| `closedlost` | Não Qualificado | Fora do ICP ou sem interesse |

**Regra de posicionamento:**
- Email pessoal verificado (`email_source` ∈ {apollo, apify, hubspot, verified}) e `icp_score ≥ 60` → stage `decisionmakerboughtin` (Mailling Funnel)
- Email geral/genérico (`oi@`, `contato@`, `marketing@`, etc.) → tratar como sem email → Linkedin Funnel (`1386309983`)
- `has_email = false` ou apenas LinkedIn URL e `icp_score ≥ 40` → stage `1386309983` (Linkedin Funnel)

> ⚠️ **Não restringir o Mailling Funnel a `email_source == "apollo"`.** Email pessoal vindo do Apify é igualmente válido. Restringir a Apollo foi a causa de deals com email Apify caírem indevidamente no LinkedIn Funnel, perdendo a associação do contato.

**Email nome-da-marca@domínio (ambíguo) → revisão manual:**
Emails cujo local-part é o próprio nome da marca/domínio (`talbor@talbor.com.br`, `candida@candidamaria.com.br`) são ambíguos: podem ser o fundador ou uma caixa funcional compartilhada. O `stage_router.py` os roteia para o stage **Lead** (`7035178`) com `needs_review: true` — **não entram na cadência de email automaticamente**. Você revisa caso a caso e move manualmente para Mailling se for um contato real. O contato ainda é criado e associado ao deal normalmente.

> ⚠️ **Atenção a dois erros fáceis de cometer:**
> 1. Mailling Funnel é o slug `decisionmakerboughtin`, **não** um ID numérico.
> 2. `1386915876` é **Churn**, não LinkedIn Funnel. Jamais colocar lead SDR nesse stage.
>
> **Nunca colocar no Mailling Funnel leads cujo único email disponível é um endereço genérico da empresa. Email pessoal do decisor é requisito.**

## Regras de Deduplicação

### Company
1. Buscar por `domain` no HubSpot
2. Se encontrar → usar ID existente (não criar duplicata)
3. Se não encontrar → criar nova Company

### Contact
1. Buscar por `email` (se disponível)
2. Se não tiver email, buscar por `linkedin_url`
3. Se encontrar → atualizar dados faltantes
4. Se não encontrar → criar novo Contact associado à Company

### Deal
1. Buscar deals associados ao Contact no pipeline `default`
2. Se deal existente no pipeline → não criar novo (atualizar se necessário)
3. Se não existir → criar novo Deal no stage correto

## Propriedades Customizadas a Registrar

> **Todas as propriedades abaixo foram confirmadas existentes no HubSpot** (jun/2026).
> Preencher apenas com dado confiável do enriquecimento (Apollo/Apify); omitir o campo se
> o dado não vier — não inventar.

### Company
- `name` — nome da empresa
- `domain` — domínio do site
- `website` — URL completa
- `industry` — setor (Moda, Beleza, etc.)
- `city`, `state`, `country` — localização
- `numberofemployees` — estimativa de funcionários *(number)*
- `annualrevenue` — faturamento anual estimado *(number)*
- `founded_year` — ano de fundação *(string; ex: "1942")* **[novo]**
- `phone` — telefone da empresa, se válido *(phonenumber)* **[novo]**
- `linkedin_company_page` — URL do LinkedIn da empresa *(API name real confirmado)*
- `description` — descrição da empresa
- `plataforma_de_ecommerce` — Shopify / NuvemShop / VTEX *(campo customizado existente)*

> **Nota:** `icp_score` não existe como propriedade no HubSpot. Registrar no campo `description` da Company no formato: `ICP Score: {N}/100 | Signals: {lista}`

### Contact
- `firstname`, `lastname`
- `email` — **somente email pessoal** (nominal; nunca genérico oi@/contato@/etc.)
- `jobtitle` — cargo
- `hs_linkedin_url` — perfil LinkedIn do contato *(API name real confirmado)*
- `phone` — telefone fixo (se disponível)
- `mobilephone` — celular do decisor, se o enriquecimento trouxer *(phonenumber)* **[novo]**
- `city`, `state`, `country` — localização do decisor, se útil **[novo]** *(state é select)*
- `hs_seniority` — senioridade do decisor *(select — ver mapa abaixo)* **[novo]**
- `hubspot_owner_id` — **dono = Marcos → `79281004`** (marcos@eyby.com.br, confirmado).
  Atribuir a TODO contact/deal criado pelo SDR. Se mudar de dono, buscar via `search_owners` **[novo]**
- `tipo_de_contato` — **sempre `e-commerce`** para decisores de lojas (opções: e-commerce, Parceiro, Afiliado). Preencher em todo contato criado/atualizado pelo fluxo SDR.

#### Mapa de senioridade (Apollo/Apify → hs_seniority)

O `hs_seniority` é um **select com opções fixas**. Mapear o `seniority`/cargo do
enriquecimento para a opção mais próxima (valores internos exatos):

| Sinal do Apollo/Apify (seniority / cargo) | hs_seniority (value) |
|---|---|
| c_suite, ceo, cfo, cmo, founder, owner, sócio, fundador, presidente | `executive` ou `owner` (sócio/fundador → `owner`) |
| vp, vice-president, vice-presidente | `vp` |
| director, diretor, head | `director` |
| manager, gerente, lead, coordenador | `manager` |
| senior, sênior, especialista sênior | `senior` |
| partner, parceiro | `partner` |
| analyst, analista, assistente, junior | `entry` ou `employee` |
| (não mapeável / desconhecido) | omitir o campo |

Opções válidas do `hs_seniority`: `vp`, `director`, `entry`, `executive`, `manager`,
`owner`, `partner`, `senior`, `employee`. **Nunca** gravar valor fora dessa lista.

> **Fica SÓ no Apollo (NÃO gravar no HubSpot):** status do email (verified/catch-all),
> email confidence, histórico profissional, foto, headcount growth, links sociais extras,
> keywords, subdepartamento. São sinais auxiliares/voláteis — usar só como contexto interno.

### Deal
- `dealname` — formato: "Afiro — {Company Name}"
- `pipeline` — `default`
- `dealstage` — ID do stage conforme regra
- `closedate` — 90 dias a partir de hoje
- `amount` — deixar vazio inicialmente
- `description` — contexto do lead: plataforma, sinais ICP, cargo do decisor *(API name: `description`)*
- `hubspot_owner_id` — **dono = Marcos** (mesmo owner_id do contact, via `search_owners`)

## Como Usar

### Sincronizar um lead
```
Sincronize o seguinte lead qualificado no HubSpot usando a skill sdr-hubspot-sync:
{JSON do lead no formato do sdr-lead-discovery}

Siga este processo:
1. Busque a empresa pelo domínio — se existir, use o ID existente
2. Busque o contato pelo email ou LinkedIn — se existir, atualize
3. Crie o Deal com nome "Afiro — {empresa}" no stage correto
4. Associe Contact → Company e Deal → Contact
5. Confirme os IDs criados
```

### Sincronizar lote de leads
```
Sincronize a lista de leads abaixo no HubSpot com deduplicação. Para cada lead:
1. Verificar duplicata (Company por domain, Contact por email/linkedin)
2. Criar ou atualizar conforme necessário
3. Criar Deal associado no stage correto
4. Retornar resumo: N criados, M atualizados, K descartados (duplicatas)
```

### Mover Deal de stage
```
Mova o Deal ID {deal_id} para o stage {stage_id} no HubSpot.
Adicione uma nota na linha do tempo: "{motivo}"
```

## Campos Customizados no HubSpot

Os campos `ecommerce_platform` e `icp_score` devem ser criados como propriedades customizadas na Company antes do primeiro uso. Use o tool `get_properties` para verificar se já existem, e `manage_crm_objects` para criar se necessário.

## Scripts Auxiliares

- `scripts/hubspot_dedup.py` — verifica duplicatas via HubSpot MCP antes de criar
- `scripts/stage_router.py` — decide o stage correto com base nos dados do lead

## Quando o decisor não puder ser validado

Se a empresa parecer aderente ao ICP, mas você **não conseguir validar um decisor real** (por exemplo: só existe `support@`, `info@`, formulário genérico, perfil social sem nome, ou a fonte de enriquecimento está indisponível), siga esta regra:

1. **Não inventar nem inferir decisor.** Nunca promover email genérico para Contact de outreach.
2. **Não criar Deal de cadência sem Contact válido.** O Deal só nasce quando existir um decisor real associado.
3. **Apresentar três caminhos claros ao usuário:**
   - criar **Company agora** e deixar pendente de enriquecimento do contato;
   - esperar e tentar novamente quando a fonte de enriquecimento voltar;
   - receber do usuário um nome/email/LinkedIn do decisor para concluir o cadastro.
4. Se o usuário optar por registrar algo imediatamente, o fallback seguro é **Company-only**, sem Contact fake e sem Deal prematuro.

Isso evita poluir o CRM com contatos genéricos ou deals que entram em cadência sem destinatário confiável.

## Notas

- Nunca criar Deal sem Company e Contact associados
- `closedate` padrão: 90 dias a partir da data de criação
- Sempre logar atividade "Email Sent" no Deal quando um email da cadência for disparado
- Deals em "Approaching" ou stages mais avançados nunca devem ser movidos para trás
