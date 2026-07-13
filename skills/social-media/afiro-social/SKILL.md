---
name: afiro-social
description: "Use when operating Afiro's social media workflow: research topics, create Instagram post/carousel plans, route Slack approvals, generate creatives, publish or schedule through Metricool MCP only after final approval, and report performance."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [afiro, social-media, instagram, slack, metricool, cron, content-ops]
    related_skills: [xurl]
---

# Afiro Social

## Overview

This skill operates the Afiro social media calendar from the current Hermes
profile. It does not create or switch profiles, does not create a new Slack
channel, does not add a custom Instagram publisher, and does not modify Hermes
core. Use the existing Slack gateway, cron jobs, `image_generate`, and the
installed Metricool MCP server.

Afiro is a platform for e-commerces to manage affiliates, creators, and
partners with AI-assisted operations. Content must educate the market, build
authority, attract store owners, and reinforce Afiro as an AI-operated partner
management solution.

## Operating Rules

- Run inside the current/default Hermes profile.
- Use the existing Slack target for approvals and delivery.
- Use Metricool MCP tools for Instagram feed/carousel scheduling, publishing,
  and metrics. Before using Metricool, discover the actual available
  `mcp_metricool_*` tools in the current session.
- Never publish or schedule anything without explicit final Slack approval.
- Never treat approval of a pauta or copy as final publication approval.
- Do not generate final creatives or call Metricool during the first execution.
- Store working history under `~/.hermes/afiro-social/` unless the user gives a
  different path.

## Internal Agents

Use these roles as a pipeline. They are responsibilities, not separate Hermes
profiles.

| Agent | Responsibility | Completion criterion |
| --- | --- | --- |
| Research & Trends | Find relevant topics, news, market pain, ICP problems, and content opportunities. | Candidate topics include source/context and connect to e-commerce partner management. |
| Editorial Strategy | Turn topics into commercial angles, objectives, funnel stages, formats, and priority. | Each selected pauta has pilar, funil, objective, format, hook, CTA, and why it matters. |
| Copywriter | Write headlines, carousel structure, body text, captions, CTAs, and test variations. | Copy is concrete, non-generic, and ready for Slack approval. |
| Creative Director | Convert approved copy into visual prompts and mobile-first creative direction. | Prompt specifies format, hierarchy, brand feel, text limits, and Affi usage only when useful. |
| Social Publisher | Validate approval, format, caption, hashtags, CTA, and then schedule/publish via Metricool MCP. | Metricool is only called after final approval and the result is recorded. |
| Performance & Learning | Pull metrics, identify winners/losers, and recommend next-week adjustments. | Weekly report names posts, metrics, learnings, repeats, avoids, and next actions. |

## Editorial Pillars

1. Dor do gestor de e-commerce:
   afiliados parados, creators sem rastreio, cupons soltos, pagamentos manuais, falta de rotina, dependencia de midia paga, dificuldade de saber quem priorizar.
2. Gestao de parceiros com IA:
   IA gerando relatorios, sugerindo campanhas, recrutando parceiros, ativando afiliados, enviando mensagens, organizando
   pagamentos.
3. Educacao sobre afiliados e creators:
   diferenca entre creator, influencer e afiliado; como criar programa de parceiros; erros comuns; creators como canal de vendas; metricas de
   performance.
4. Provas e exemplos praticos:
   antes/depois, comandos para IA, rotinas semanais, metricas que importam, comparacao entre planilha e Afiro.
5. Conversao:
   convite para testar a Afiro, diagnostico do programa de parceiros, demo da plataforma, CTA para reuniao, beneficios para Shopify, Nuvemshop e VTEX.

## Voice And Positioning

Write in Portuguese for Afiro unless the user requests another language.

Prefer:
- "Afiliados parados nao vendem"
- "Creator sem rastreio vira custo, nao canal"
- "Seu programa de parceiros precisa de rotina"
- "A IA mostra quem ativar, quem reter e qual campanha criar"
- "Pare de operar afiliados em planilha"

Avoid:
- "potencialize seus resultados"
- "transforme seu negocio"
- "revolucione sua operacao"
- "IA do futuro"
- generic marketing-digital advice detached from affiliates, creators,
  partners, or e-commerce operations.

Keep claims concrete. If a metric, benchmark, customer case, or platform
capability is not verified, frame it as a recommendation, example, or
hypothesis, not as a fact.

## Cadence And Formats

Daily target: 2-3 posts.

Default mix:
- 1 educational post
- 1 pain/benefit post
- 1 conversion or practical proof post

Weekly guide:
- Monday: pain + weekly planning
- Tuesday: education + practical example
- Wednesday: applied AI + carousel
- Thursday: proof/benefit + CTA
- Friday: checklist + diagnostic
- Saturday: light content or quick insight
- Sunday: optional only if there is a strong pauta

Formats:
- Static post and carousel: 1080x1440 (3:4 aspect ratio) for all feed
  creatives.
- Never generate 2:3 or 9:16 images for feed/carousel; Metricool requires
  width ÷ height between 0.75 and 1.91, and 3:4 (0.75) is the vertical limit.
  Validate every image dimension before sending to Metricool.
- Reels/stories: suggest as opportunities only; do not prioritize unless asked.

## Approval Workflow

Use Slack as the approval surface. For every item, send a clear block of text
with an ID and current status.

Pauta approval message must include:
- ID
- suggested title
- objective
- editorial pillar
- funnel stage: topo, meio, or fundo
- suggested format
- 2 hook options, with one marked as recommended
- summarized copy
- CTA
- justification

Accept only these decision patterns:
- `aprovado`
- `ajustar: <specific requested change>`
- `rejeitado`

Approval gates:
1. Pauta approved -> write full copy.
2. Copy approved -> create visual prompt and generate creative.
3. Creative approved -> ask for final publication approval.
4. Final approval received -> use Metricool MCP to schedule/publish.

If any approval is missing or ambiguous, stop and ask in Slack. Do not infer
approval from silence, positive comments, or earlier-stage approval.

When a Slack reply contains `aprovado`, `ajustar:`, or `rejeitado` for known
item IDs, do not ask what to do next — continue the pipeline at the
corresponding gate immediately: pauta approved means write the full copy and
submit it for copy approval; copy approved means generate the visual prompt
and creative; creative approved means ask for final publication approval.
Handle one carousel at a time. Read `~/.hermes/afiro-social/content.jsonl` to
recover each ID's current status if this session lacks prior context.

## First Execution Mode

When this skill runs for the first time, or when the prompt says "primeira
execucao", do not generate final creatives, do not schedule, and do not publish.

Do only this:
1. Validate operational readiness:
   - Slack delivery target is available.
   - `image_generate` is available.
   - Metricool MCP tools are loaded.
   - Metricool tools can support Instagram feed/carousel and metrics, based on
     actual discovered tool names/descriptions.
2. Prepare:
   - 7-day editorial calendar.
   - 15 pautas.
   - 5 static post examples.
   - 2 carousel examples.
3. Send the package to Slack for approval.
4. Record the package locally if file/terminal tools are available.
5. Stop with a clear "aguardando aprovacao" message.

## Daily Cron Behavior

Daily cron prompts should ask for "daily planning mode". In that mode:
1. Read `~/.hermes/afiro-social/learnings.md` (approver feedback and burned
   angles) before writing anything, and apply it. When new `ajustar:` or
   `rejeitado` feedback arrives, append the lesson there.
2. Research current topics and evergreen angles.
3. Generate more candidates than needed.
4. Select 2-3 posts for the day using commercial potential and specificity.
5. Send the selected pautas to Slack for approval.
6. Save an event for each pauta in local history.
7. Stop. Do not generate final creatives or publish during the pauta-selection
   cron run unless the prompt explicitly says this is a continuation with
   approvals already recorded.

## Weekly Cron Behavior

Weekly cron prompts should ask for "weekly report mode". In that mode:
1. Use Metricool MCP metrics tools, if available, to pull recent Instagram
   performance.
2. Summarize:
   - posts published
   - top 3 posts with WHY they worked (hook, format, theme, timing)
   - bottom 3 posts with the lesson to extract
   - reach/impressions/likes/comments/saves/shares/profile clicks/link clicks
     where available; weigh comments above likes, and treat saves as the key
     carousel metric
   - themes and formats that performed best
   - recommendations for the next week as conditional actions: if engagement
     is low, test different hooks, formats, or posting times; if reach is
     declining, reduce links in captions and increase comment engagement
3. Once a month (first weekly report of the month), also run a viral
   reverse-engineering pass: find 10-20 high-engagement Instagram/LinkedIn
   profiles in e-commerce, creator economy, or martech; review their recent
   top posts; extract hook and format patterns that repeat; append the
   validated patterns to `~/.hermes/afiro-social/learnings.md` as input for
   daily planning.
4. Send the report to Slack.
5. Save a report event locally.

If Metricool metrics tools are not available, report the missing capability and
still provide qualitative learnings from the local history.

## Creative Direction

Creative prompts for `image_generate` must specify:
- The Affi mascot as the visual anchor of every creative, using the reference
  image at `~/.hermes/afiro-social/affi-reference.png` (pass it as
  `image_url`/`reference_image_urls` if the tool supports references; if the
  file is missing, stop and ask for it in Slack before generating). Affi is a
  friendly white robot with a black glowing face, green smiling expression,
  green side accents, green chest button, and green cape. Affi must interact
  with the content —
  pointing to a metric, holding a card, comparing dashboards, reacting — not
  stand decoratively beside it; vary pose and action across slides and posts.
- Never create, recreate, simulate, or insert the Afiro logo, logo-like
  marks, invented wordmarks, or fake app icons. The logo is absent from
  generated creatives unless the user explicitly provides the final asset and
  asks to place it.
- Size 1080x1440 (3:4) for static posts and every carousel slide.
- Afiro green as the main brand signal.
- Premium SaaS look, clean layout, high contrast, mobile-first readability.
- Minimal text on the image.
- Clear hierarchy: one main idea per slide.
- No generic "AI robot brain" visual language.

Creative sequencing:
- Generate images for ONE pauta at a time — never batch creatives for
  multiple pautas in the same step.
- Only start the next pauta's creatives after the previous pauta's creative
  has received explicit approval in Slack.

For carousels, draft slide-by-slide:
- cover hook
- 3-6 content slides
- final CTA slide

## Metricool Rules

Before calling Metricool:
- Confirm the item has final approval.
- Confirm the creative file/URL and caption are final.
- Confirm format is static or carousel.
- Confirm target is Afiro Instagram.
- Confirm schedule time or publish-now instruction.

Use the installed Metricool MCP server for:
- Instagram feed publishing or scheduling.
- Carousel publishing or scheduling.
- Pulling metrics for weekly reports.

Do not use the in-repo Instagram platform adapter for feed/carousel publishing;
that adapter is for Instagram Direct Messages.

## Local History

Use `~/.hermes/afiro-social/content.jsonl` for append-only events when file or
terminal tools are available. Each event should include:
- timestamp
- content_id
- status
- pilar
- funil
- format
- title
- caption or copy reference
- creative path or URL
- Slack thread/message reference if known
- Metricool post/schedule id if known
- Instagram URL if published

Statuses:
- `drafted`
- `pauta_approved`
- `copy_approved`
- `creative_approved`
- `final_approved`
- `scheduled`
- `published`
- `needs_adjustment`
- `rejected`

If history cannot be written, mention the limitation in the Slack delivery and
continue without publishing.

## Verification Checklist

- [ ] Running in the current/default profile; no profile switching.
- [ ] Slack delivery target is the existing connected Slack surface.
- [ ] Metricool MCP tools were discovered before use.
- [ ] No Metricool publish/schedule call occurs without final approval.
- [ ] First execution stops before creative generation and publication.
- [ ] Daily cron only submits pautas unless continuing an approved item.
- [ ] Weekly cron reports metrics or clearly states why metrics are unavailable.
- [ ] Local history is appended when tools allow it.
