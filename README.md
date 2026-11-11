# RevenueOS

Claude-powered agents for sales and marketing. They score leads, research accounts on the web, write personalized outreach and keep the CRM current. A human approves every message before it leaves.

This covers **Phase 1** (prospecting and outreach), **Phase 2** (replies, meetings, deals, proposals, CRM hygiene) **Phase 3** (campaigns, content, ABM, competitor intel, buying signals, lead sourcing) and **Phase 4** (deal strategy, forecasting, attribution, A/B testing, and a learning loop).

New to RevenueOS, or not technical? Start with the [plain-English guide](docs/USER_GUIDE.md).

## What's in it

| Agent | Model | What it does |
|---|---|---|
| Orchestrator (Copilot) | Opus 5.5 | Takes plain-language requests from reps, plans the work, delegates to the agents below, often in parallel, and reports back |
| Lead Scorer | Haiku 5.5 | Scores fit and intent against your ICP, assigns a tier from A to D, explains its reasons and recommends a route |
| Account Researcher | Sonnet 5.5 + web search/fetch | Builds a sourced account brief: priorities, pains, recent news, best angle, matching proof points, likely objections |
| Outreach Writer | Sonnet 5.5 | Writes a multi-step email/LinkedIn sequence (with optional A/B test) grounded in the brief and the knowledge base, and queues it for approval |
| Reply Triage | Haiku 5.5 → Sonnet 5.5 | Sorts each reply (interested, question, objection, referral, not now, out of office, unsubscribe, wrong person), updates the pipeline, adds referred people as leads, and drafts the answer for approval |
| Meeting Prep | Sonnet 5.5 + web | One-page call brief: objective, people in the room, agenda, discovery questions, proof points, objections, the ask |
| Call Analyst | Sonnet 5.5 | Turns a transcript or notes into MEDDICC, deal stage, health, risks and next steps. Creates or updates the deal (and the Salesforce Opportunity) and drafts the follow-up email |
| Pitch & Proposal Builder | Sonnet 5.5 | Writes a tailored proposal and pitch deck from the brief, call summaries and knowledge base. Renders an editable **.docx** and **.pptx** |
| CRM Hygiene | rules + Haiku 5.5 | Finds duplicates (with one-click merge), missing data, quiet leads and neglected deals. Fills missing industry/headcount only for well-known companies |
| Campaign Strategist | Opus 5.5 | Turns a goal into a plan: KPIs, audience (sized against your database), key message, offer, channel and budget split, weekly timeline, content plan, sales play. Adds matching contacts as campaign members (and creates a Salesforce Campaign) |
| Content Studio | Sonnet 5.5 + web | Blog posts, LinkedIn posts, ads, landing pages, newsletters, case studies, one-pagers, in brand voice and grounded in the knowledge base. Drafts go to Approvals |
| ABM | Sonnet 5.5 | For one target account: personalized landing page, three LinkedIn ad variants, a one-pager for the champion, and a role-specific email for every contact there |
| Competitor Intel | Sonnet 5.5 + web | Sourced battlecard: strengths, weaknesses, recent moves, pricing, how we win, landmine questions, objection responses. The latest copy goes into the knowledge base so every agent uses it |
| Signal Detector | Sonnet 5.5 + web | Scans your top accounts for buying triggers (new executive, funding, M&A, hiring, initiatives) with source, strength and a specific recommended action |
| Deal Strategist | Opus 5.5 | Reviews a deal like a skeptical VP of Sales: honest win probability and forecast category, stakeholder map and missing roles, risks, 3-5 next best actions, a talk track, and (when useful) a drafted email |
| Forecast Analyst | Opus 5.5 | Interprets the computed quarter forecast (commit, best case, weighted, coverage, slipped deals) for leadership: the forecast call, the deals that decide the quarter, risks and actions. It never computes numbers itself |
| Learning loop | Opus 5.5 | Studies sent emails and their real outcomes plus A/B results, and writes the **learned playbook**. Every writing agent reads it. It only runs with enough data (20+ sends, 3+ replies) |
| Lead Sourcer | Sonnet 5.5 + web | Finds real ICP-fit companies with evidence of fit and target titles, skipping accounts you already have. It never invents people or emails |

**Workflows**
- **Inbound (speed-to-lead):** `POST /api/webhooks/inbound`. Scores the lead; for A/B tier it also researches the account and drafts a reply into Approvals within minutes.
- **Outbound batch:** select leads in Pipeline, then **Run outbound**. Leads are scored, weak fits are skipped, and the rest are researched and sequenced.
- **Replies:** `POST /api/webhooks/reply` pauses the sequence at once (and honours explicit opt-outs immediately), then Reply Triage decides what happens next. Out-of-office auto-replies resume the sequence.
- **Deal flow:** on a lead's page, **Prep a meeting** before the call, **Analyse a call** after it (this creates the deal and a follow-up draft), then **Build proposal + deck**. The **Deals** page shows MEDDICC, health, risks and next steps.
- **CRM hygiene:** a rule-only report is written daily at no AI cost. Run it on demand from the **CRM hygiene** page.
- **Sequences:** each multi-step sequence is an *enrollment*. Step delays count from when the first message was sent; a step never goes before the one before it, and follow-ups only go out in business hours in the contact's time zone (`SEND_WINDOW_*`, `DEFAULT_TIMEZONE`, or the lead's own time zone on their page). A reply pauses the sequence at once; Reply Triage resumes it after an out-of-office return date or stops it for any real answer. LinkedIn and call steps become dated items on the **Tasks** page. Pause, resume or stop a sequence from the lead's page.
- **Sending service:** set `EMAIL_PROVIDER=sendgrid` or `postmark` for HTML email, open/click tracking and delivery events. Hard bounces, spam complaints and provider unsubscribes suppress the address and stop the sequence automatically.
- **Campaigns:** describe the outcome on the **Campaigns** page, review the plan, then **Draft all content**. Pieces arrive in Approvals (filter: marketing content) and are published by a person.
- **Revenue insights:** the **Insights** page shows the quarter forecast, pipeline by source, campaign results, reply rates by agent and sequence step, A/B tests and the learned playbook. **Deals** has a **Review** button per deal. Set `QUARTERLY_TARGET` for attainment and coverage. Set `REVIEW_INTERVAL_DAYS` to run deal reviews, the forecast and the learning loop automatically.
- **A/B testing:** when a sequence is written with an A/B test, each contact is randomly assigned one variant (the other is auto-held out), so results are unbiased. A winner is declared only with 30+ sends per variant and p < 0.05 on positive replies.
- **Market intel:** scan for buying signals, build battlecards, and source new accounts. Add contacts for sourced accounts with **Import contacts CSV**, which recognises Apollo, ZoomInfo, Sales Navigator and Salesforce export headers. Set `SIGNAL_SCAN_INTERVAL_DAYS` to run signal scans automatically (off by default because it costs AI and web-search spend).

**Customer context: everything the team knows, in one timeline**

Every assistant reads one timeline per account, built from all sources (shown on each lead's page under **Customer timeline**):

| Source | How it connects | What happens |
|---|---|---|
| Slack | Slack app → `POST /api/integrations/slack/events` (signed with `SLACK_SIGNING_SECRET`). Reads **only** channels opted in on the **Integrations** page | Messages are summarised per account (Haiku) into commitments, objections, stakeholders and risks. Internal channels are marked internal; shared (Slack Connect) channels are the customer's own words |
| Zoom | Webhook `POST /api/integrations/zoom` (`recording.transcript_completed`, signed with `ZOOM_WEBHOOK_SECRET`) | The transcript is downloaded, matched to the contact and run through the Call Analyst (deal updated, follow-up drafted, call added to the timeline) |
| Teams, Google Meet, Gong, uploads | `POST /api/integrations/transcripts` with attendees + transcript (`X-Webhook-Secret`) | Same as Zoom. Calls that can't be matched wait under "Needs a home" for a person to assign |
| Calendar | `POST /api/integrations/calendar-events` (`X-Webhook-Secret`) | Customer meetings are matched to contacts; a Meeting Prep brief is written `MEETING_PREP_HOURS_BEFORE` (default 24) hours before |
| **Gmail / Outlook** | Each rep clicks **Connect Google** or **Connect Microsoft 365** on Integrations (read-only OAuth; tokens encrypted with `SECRET_KEY`) | Every 15 minutes: replies from known contacts pause their sequence and go to Reply Triage; emails reps send themselves join the timeline. Mail with no known contact is never stored |
| **Google / Outlook calendar** | Same connection | Customer meetings are matched to contacts and prepped automatically |
| **Teams and Google Meet transcripts** | Same connection (Graph online-meeting transcripts; Meet REST API) | 10+ minutes after a customer meeting ends, its transcript is fetched and run through the Call Analyst |
| **Gong** | `GONG_ACCESS_KEY` / `GONG_ACCESS_SECRET` | New calls with transcripts are imported every 15 minutes and analysed; only external participants are matched to contacts |
| Rep notes | Note box on the timeline (internal by default) | |

**Slack, both ways:** set `SLACK_BOT_TOKEN` and `SLACK_APPROVALS_CHANNEL`, and every new draft is posted to that channel with **Approve & send / Reject / Edit in RevenueOS** buttons (Interactivity URL: `/api/integrations/slack/interactions`). Reps can also type `/revenueos brief me on Siemens before my 3pm` (slash command URL: `/api/integrations/slack/commands`); the Copilot answers privately in Slack. Anyone in the approvals channel can approve, so keep it to the sales team.

**Deeper personalisation**
- **Person dossier** (**Research person** on a lead): the person's public, professional footprint with sources: talks, interviews, articles, priorities and talking points. No private or sensitive information, and LinkedIn is not scraped. The writer uses it for the opener, and outbound batches build it automatically for A/B-tier leads.
- **Committee outreach** (**Committee outreach** on a lead): one account narrative, then a role-specific, staggered sequence for every contact at the account (champion, economic buyer, technical, finance). It names roles nobody covers yet.

**Proving it works: holdout groups.** Outbound batches randomly hold back `HOLDOUT_PCT` (default 20%) of *eligible* leads from AI outreach. **Insights** compares meeting rate, deals and pipeline between the two groups and reports a significance test once each group has 30+ contacts. Held-out leads are protected: the writer refuses them unless a rep clicks **Release** (which removes them from the statistics).

Privacy: internal team text is never shown to customers. A guardrail **blocks** any draft that repeats 7+ words of internal discussion. Raw text is deleted after `CONTEXT_RETENTION_DAYS` (default 90); summaries are kept. Native Gmail/Outlook and Google/Outlook calendar sync come next; until then, calendar events and Teams/Meet transcripts arrive through the generic endpoints (for example from Power Automate or Apps Script).

**Checking the facts**
- **Sources the AI really read:** every page a web search returned or an assistant opened is recorded. A brief's news items and sources, or a dossier's quotes, that point to a page nobody opened are removed (they are likely invented). A draft citing such a page gets a "no assistant actually opened this page" warning.
- **Claims must match their source:** a claim citing a knowledge-base document must actually be in it (its figures, or most of its words); otherwise the draft is flagged.
- **Every customer-facing draft is graded:** call follow-ups, deal-strategy emails and marketing content (with a content rubric) get a score and warnings too. Facts the grader can't find in the evidence are flagged. If grading fails, the draft is marked "not graded" instead of being lost.
- **Better knowledge search:** word forms match ("forecasting" finds "forecasts"), long documents are split sensibly with overlap, weak matches are dropped, and a fast model re-ranks the top matches (`KB_RERANK`).
- **Fresh research:** briefs older than `BRIEF_MAX_AGE_DAYS` and dossiers older than `DOSSIER_MAX_AGE_DAYS` are refreshed before a sequence is written.

**Daily work**
- **My day:** past-SLA leads, hot replies, tasks due, drafts waiting and the next 48 hours of customer meetings on one page. Call Analyst next steps and Deal Strategist actions become tasks automatically.
- **Booking link:** set `BOOKING_URL` and replies to interested prospects offer it.
- **UTM tags:** links to your own site in sent emails and approved content carry `utm_source=revenueos`, the medium, campaign and draft id.
- **Funnel and SLA:** contacts move lead → MQL → SQL → opportunity → customer automatically (`MQL_SCORE`). Insights shows the funnel and how often MQLs get a human touch within `MQL_SLA_HOURS`.

**Measured quality**
- **Prompt versions:** every assistant's instructions are versioned automatically (`prompt_versions`); runs and drafts record the version, and Insights compares draft quality by version. Admins add workspace-specific instructions per assistant on **AI governance**.
- **Learning loop v2:** learns from sent emails and outcomes **and** from how reviewers edited and why they rejected drafts. A new playbook is a **proposal** until an admin approves it on Insights; the previous version can be restored with one click. Needs `LEARN_MIN_SENT` (50) sends and `LEARN_MIN_REPLIES` (10) replies.
- **Grader check:** Insights shows whether high personalisation scores really get sent unedited more often than low ones.
- **Evals:** `python -m evals.run` runs the real assistants on golden cases (lead scoring, reply triage, the grader, a full sequence with an AI judge) in a throwaway database and fails when a suite drops below `evals/baseline.json`. CI (`.github/workflows/ci.yml`) runs the tests and type-check on every push and the evals on demand or on PRs labelled `prompt-change`.

**Sales management** (**Sales setup** page)
- **Territories and routing:** new leads get an owner automatically, by country, industry and size, round-robin within the territory; a lead at an account that already has an owner goes to that owner.
- **Quotas:** per rep per quarter; Insights shows attainment, pipeline closing this quarter, coverage and win rate per rep.
- **Win/loss:** closing a deal requires a reason; competitors mentioned on calls are recorded on the deal. Insights shows why you win and lose, overall and per competitor, and the Deal Strategist sees your record against the competitors in each deal.
- **Products and line items:** a price list, line items on deals (the amount follows), and proposals that quote only those products and prices.

**Data and nurture**
- **Enrichment** (`ENRICHMENT_PROVIDER=apollo`): fills only empty fields, every scheduler tick and on demand (**Enrich** on a lead).
- **Email verification** (`zerobounce` / `neverbounce`): checked before the first email (cached 90 days). Invalid addresses are blocked, risky ones warned. Malformed addresses are always blocked.
- **Nurture** (**Nurture** page): email series approved once per step, sent to every member in business hours with merge fields; members leave when they become an MQL, reply or opt out.
- **Multi-touch attribution:** first touch, last touch, linear and W-shaped credit for every deal on Insights.

**Reach and customers**
- **Website visits:** put the snippet from **Market intel** on your site. Visitors who came from one of our emails are recognised; viewing pricing, demo or contact pages (`INTENT_PAGES`) creates a buying signal and counts as engagement. The Lead Scorer sees recent signals.
- **Intent data:** Bombora, G2, 6sense and others via `POST /api/webhooks/intent` or a CSV upload. Medium and strong topic surges become signals; strong ones on unknown companies can create the account (`INTENT_CREATE_ACCOUNTS`).
- **Events and webinars** (**Events** page): registrants become leads; after the event, attendees and no-shows go into different nurture series and engaged attendees become tasks.
- **Customers** (**Customers** page): won deals with recurring products become contracts; renewal deals and tasks open `RENEWAL_LEAD_DAYS` before the end; a health score raises churn-risk signals; upsell ideas list products the customer doesn't have.
- **LinkedIn publishing:** approved LinkedIn posts get a **Publish to LinkedIn** button (company page). Ad spend per campaign (entered or imported from Campaign Manager / Google Ads CSV) shows cost per meeting and ROI on Insights.

**Running it safely**
- **Notifications:** hot replies go straight to the owner (Slack DM or email), MQLs waiting past the SLA alert their owner once, each rep gets a morning digest (`DIGEST_HOUR`), and failed sends or jobs alert the team (`ALERT_EMAIL`, Slack).
- **Approvals at scale:** bulk approve or reject, oldest-first sorting and waiting time on each draft.
- **Activity log** page: every assistant run and job with its steps, time, cost and errors. Every response carries `X-Request-ID`; `SENTRY_DSN` turns on error tracking.
- **Sign-in:** two-factor sign-in (authenticator apps), lockout after 5 failed attempts, admin password reset, and single sign-on with any OpenID Connect provider. `python -m revenueos.rotate_keys` re-encrypts stored secrets after changing `SECRET_KEY`.
- **Privacy:** a personal one-click unsubscribe link in every email (and `List-Unsubscribe-Post`), lawful basis per contact (required in `CONSENT_REQUIRED_COUNTRIES`), data export and erasure on **Settings**, and automatic clean-up of AI run logs and rejected drafts.
- **Dependencies:** `backend/requirements.lock` pins the exact versions this release was tested with.

**Personalisation gate:** every email and LinkedIn draft is scored 0–100 by a grader against the brief and relationship history. Below `PERSONALISATION_THRESHOLD` (default 60) the writer gets specific feedback and rewrites once. If the rewrite is still below, it is queued with a warning. The score and the facts it used are shown in Approvals.

**AI governance** (**AI governance** page): monthly budgets for total AI spend and per assistant, a model choice per assistant (web-searching assistants can't be set to Haiku), and on/off switches. A run is refused once a budget is reached, and a warning is posted to the Slack approvals channel at 80%.

**Brand safety at depth**
- **Sources on every claim:** writers list each factual claim with its source (a knowledge-base document, a URL from the brief or dossier, or what the customer said). Approvals shows them beside the draft and warns on any source it can't verify.
- **No sensitive personal details:** drafts that mention someone's family, health, home, beliefs or age are blocked.
- **Rule packs:** a knowledge-base document of type `rules` with lines `BANNED: <phrase>` (blocks the draft), `REQUIRED: <wording>` (warns if missing, e.g. a disclaimer) and `TONE: <instruction>` (given to every writer).

**Safety and trust**
- Agents cannot send anything. They can only create drafts in the **Approvals** inbox, and only `services/approvals.py` sends.
- Guardrails run on every draft. They block suppressed or missing recipients and unfilled placeholders, and warn on any **figure not found in the knowledge base or brief** (to catch invented results), spam phrases and over-long copy.
- Daily and per-domain send caps, an unsubscribe footer with your physical address, and a `List-Unsubscribe` header.
- LinkedIn messages are copy-and-send by the rep, which respects LinkedIn's terms.
- Every agent run is logged with tool calls, tokens and cost (`agent_runs`), and the dashboard shows cost per agent.

## Quick start (local, no Docker)

```bash
cd backend
pip install -r requirements.txt
```

```bash
cd backend
python -m revenueos.seed
```

```bash
cd backend
ANTHROPIC_API_KEY=sk-ant-... COMPANY_NAME="Your Company" uvicorn revenueos.main:app --reload
```

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000. Without Salesforce or SMTP settings, the app uses its built-in CRM and **dry-run email**: approved emails are logged, not sent.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Then load the sample data once:

```bash
docker compose exec api python -m revenueos.seed
```

## Make it yours: the knowledge base

The agents have no industry-specific code. What they know comes from **Knowledge base** in the UI, or from `backend/seed_data/kb_<category>__<title>.md`:

| Category | Put in |
|---|---|
| `offering` | What you sell and the outcomes it delivers |
| `case_study` | **Approved** customer stories with real numbers. Agents may only cite these |
| `icp`, `persona` | Who you sell to and what each buyer cares about |
| `brand_voice` | How you write |
| `pricing`, `objection`, `competitor` | Guidance reps use today |

The sample content is marked `SAMPLE CONTENT`. Replace it before real use. The sample leads are fictional people with `@example.com` addresses.

## Clients, sign-in and onboarding

**Client workspaces.** One RevenueOS deployment serves many clients. Each **workspace** has its own database
(a separate SQLite file, or a separate Postgres schema), its own settings (company details, CRM and its
credentials, email, Slack, Gong, pilot options), its own knowledge base and its own people. Requests and
background jobs always run inside one workspace, so one client's data never appears in another's.
The default workspace is the original `DATABASE_URL`, so a single-company install keeps working unchanged.

**Sign-in.** Until the first account exists the app is open, as before. Open `/login` and create the
**owner** account; from then on every page and API call needs sign-in (passwords hashed with scrypt; sessions
expire after 14 days and end on sign-out). Roles: **owner** (platform: creates client workspaces),
**admin** (a workspace's settings and people), **member** (everything else). `API_TOKEN` still works as a
service credential (e.g. for the MCP server) on the default workspace.

**Per-workspace settings** (**Settings** page) override the environment for that workspace only. Secrets are
encrypted with `SECRET_KEY` and never shown again. Webhooks name their workspace with `?ws=<slug>`; the
Integrations page shows each workspace's exact URLs.

**Onboarding** (**Setup checklist**): company details → connect and test the CRM → the four knowledge documents
that matter most (and remove the samples) → bring in leads → connect email/calendar/Slack (optional) →
start a pilot with a control group.

**Proving it (pilot kit)**
- **Holdout lift** (Phase B) on the Insights page.
- **Blind test** (**Blind test** page): for leads that have a RevenueOS draft, paste the email your team (or
  another tool) would send; reps pick the better one without knowing which is which. Results use a two-sided
  sign test and need 30+ decided comparisons to call a winner.
- **Return on AI spend**: AI spend, cost per meeting, AI-influenced deals, pipeline and won revenue (a deal is
  AI-influenced when RevenueOS emailed someone at the account before the deal was created), plus whether
  higher personalisation scores earn more positive replies.
- **Pilot report**: `GET /api/pilot/report` (JSON) or **Download the pilot report** (Markdown) on Insights.

## Works with any CRM

RevenueOS sits on top of whichever CRM is your system of record (`CRM_PROVIDER`):

| CRM | Setup | What syncs |
|---|---|---|
| **Salesforce** | `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN` (`SF_DOMAIN=test` for a sandbox) | Lead import, Tasks, Lead status/rating, Opportunities, Campaigns + members |
| **HubSpot** | `CRM_PROVIDER=hubspot`, `HUBSPOT_TOKEN` (private app: contacts + deals read/write) | Contact import (lead status NEW/OPEN), notes, `hs_lead_status`, deals in the default pipeline. Campaigns need Marketing Hub and stay in RevenueOS |
| **Dynamics 365 Sales** | `CRM_PROVIDER=dynamics`, `DYNAMICS_URL`, `DYNAMICS_CLIENT_ID`/`SECRET` (app user with a sales role) | Open-lead import, tasks, open-lead status, opportunities (won/lost via the close actions), campaigns |
| **Zoho CRM** | `CRM_PROVIDER=zoho`, `ZOHO_CLIENT_ID`/`SECRET`/`REFRESH_TOKEN` (self client; set `ZOHO_ACCOUNTS_URL`/`ZOHO_API_DOMAIN` outside the US) | Lead import (Not Contacted / Attempted), notes, lead status, deals, campaigns |
| **Pipedrive** | `CRM_PROVIDER=pipedrive`, `PIPEDRIVE_TOKEN`, `PIPEDRIVE_DOMAIN` | Leads Inbox import (with person and organisation), notes, deals (won/lost always; stage ids via `CRM_FIELD_MAP`). Pipedrive leads have no status field |
| **None** | default | RevenueOS's own database is the CRM |

All adapters implement one contract (`tools/crm/base.py`) and pass the same contract tests. Every 15 minutes RevenueOS also **pulls** new open leads and deal changes reps made in the CRM, so the two never drift (`POST /api/crm/sync` to pull now). Stage and status mappings can be overridden with `CRM_FIELD_MAP`, e.g. `{"deal_stage": {"proposal": "contractsent"}}`.

**Side panel inside the CRM:** `/embed?crm=salesforce&id=<record id>` (or `?email=`) shows the rating, research angle, dossier, drafts with Approve/Reject and the customer timeline. Install templates for a Salesforce Lightning component, a HubSpot app card and a Dynamics web resource are in `crm-panels/`. Allowed framing domains: `EMBED_ALLOWED_ORIGINS`.

**Use RevenueOS from other AI assistants (MCP):** `python -m revenueos.mcp_server` (stdio, for Claude Desktop / Claude Code) or `python -m revenueos.mcp_server --http 8001` (streamable HTTP at `/mcp`, requires `Authorization: Bearer <API_TOKEN>`). It offers 13 tools: lookups (leads, timeline, knowledge base, pipeline, deals) and the research/drafting/analysis agents. Drafting tools only queue drafts for approval. Check each assistant's current MCP support before relying on it (Copilot Studio, Agentforce, etc.).

### Salesforce details

Set `SF_USERNAME`, `SF_PASSWORD` and `SF_SECURITY_TOKEN` (and `SF_DOMAIN=test` for a sandbox). Then:
- **Import open leads from CRM** in Pipeline pulls open, unconverted Leads.
- Sending an approved email logs a completed `Task` on the Lead or Contact.
- Scoring and status changes update `Lead.Status` and `Lead.Rating`.
- Call analysis and proposals create or update the `Opportunity` (stage, amount, close date, next step, MEDDICC in the description). The Account must already exist in Salesforce; otherwise the deal stays local.

Start with a sandbox org.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/copilot {message}` → `{run_id}` | Ask the orchestrator; poll `GET /api/runs/{id}` |
| `GET/POST /api/leads`, `GET /api/leads/{id}` | Pipeline |
| `POST /api/leads/{id}/score \| research \| sequence` | Run one agent (returns `run_id`) |
| `POST /api/leads/batch-outbound {contact_ids}` | Outbound workflow |
| `GET /api/approvals?status=pending`, `POST /api/approvals/{id}/approve \| reject` | Human review. The reviewer is the signed-in user; reject takes a `reason` from `GET /api/approvals/reject-reasons`. Edits keep the AI original (`ai_subject`, `ai_body`, `edit_pct`) |
| `GET /api/enrollments?contact_id=`, `POST /api/enrollments/{id}/pause \| resume \| stop`, `PUT /api/leads/{id}/timezone` | Sequences |
| `GET/POST /api/tasks`, `PATCH /api/tasks/{id}` | Tasks (calls, LinkedIn steps, next steps) |
| `POST /api/webhooks/email/sendgrid \| postmark` | Delivery events: bounces, complaints, unsubscribes, opens, clicks |
| `GET /api/my-day?mine=` | My day: SLA breaches, hot replies, tasks due, drafts waiting, meetings |
| `GET /api/insights/funnel` | Lifecycle funnel and MQL response-time SLA |
| `GET /api/insights/playbook/versions`, `POST /api/insights/playbook/{id}/approve \| reject`, `POST /api/insights/playbook/rollback` | Playbook proposals, publishing, rollback (admin) |
| `PUT /api/governance/{agent}/prompt`, `GET /api/governance/prompts` | Workspace prompt additions and prompt version history |
| `GET/POST/PUT/DELETE /api/territories`, `POST /api/routing/run`, `PUT /api/leads/{id}/owner` | Territories and lead routing |
| `GET/PUT /api/quotas`, `GET /api/insights/reps`, `GET /api/insights/win-loss`, `GET /api/deals/close-reasons` | Quotas, rep performance, win/loss |
| `GET/POST/PUT /api/products`, `GET/POST /api/deals/{id}/lines`, `DELETE /api/deals/{id}/lines/{line}` | Price list and deal line items |
| `GET/POST /api/nurture`, `POST /api/nurture/{id}/steps \| enroll \| pause \| resume` | Nurture series |
| `POST /api/leads/{id}/enrich \| verify-email`, `GET /api/data/providers` | Enrichment and email verification |
| `GET /api/insights/attribution/multi-touch` | Multi-touch attribution |
| `POST /api/approvals/bulk`, `GET /api/approvals?order=oldest` | Bulk approve/reject, waiting time |
| `POST /api/auth/mfa/setup \| enable \| disable`, `GET /api/auth/oidc/start`, `POST /api/workspace/users/{id}/password` | Two-factor, SSO, password reset |
| `PUT /api/leads/{id}/lawful-basis`, `POST /api/privacy/export \| erase`, `GET/POST /api/public/unsubscribe` | Privacy |
| `GET /api/public/t.js`, `POST /api/public/track`, `GET /api/visits`, `GET /api/tracking/snippet` | Website visit tracking |
| `POST /api/webhooks/intent`, `POST /api/intel/intent/import` | Intent data |
| `GET/POST/PUT /api/events`, `POST /api/events/{id}/registrations \| attendance \| follow-up`, `POST /api/webhooks/event-registration` | Events and webinars |
| `GET /api/customers`, `POST/PUT /api/contracts`, `POST /api/customers/renewals/run` | Customers, renewals, health |
| `POST /api/approvals/{id}/publish`, `POST /api/campaigns/{id}/spend`, `POST /api/campaigns/{id}/spend/import` | LinkedIn publishing, ad spend |
| `GET /api/insights/quality` | Draft accuracy: approved-without-edits rate, average edit size, reject reasons, per assistant |
| `GET/POST /api/kb`, `POST /api/kb/upload`, `GET /api/kb/search?q=` | Knowledge base |
| `POST /api/webhooks/inbound \| reply \| unsubscribe` | Website forms and email provider events (`X-Webhook-Secret` header) |
| `POST /api/leads/{id}/meeting-prep \| call-analysis \| proposal` | Deal-stage agents (return `run_id`) |
| `GET /api/documents`, `GET /api/documents/{id}`, `GET /api/documents/{id}/file` | Prep briefs, call summaries, proposals, decks, reports |
| `GET /api/deals`, `PATCH /api/deals/{id}` | Opportunities |
| `POST /api/hygiene/run`, `GET /api/hygiene/latest`, `POST /api/hygiene/merge-contacts` | CRM hygiene |
| `POST /api/campaigns`, `GET /api/campaigns/{id}`, `POST /api/campaigns/{id}/content`, `PATCH /api/campaigns/{id}` | Campaign Strategist + content generation |
| `POST /api/content`, `POST /api/accounts/{id}/abm` | One-off content; ABM pack |
| `POST /api/intel/battlecard`, `GET /api/intel/battlecards` | Competitor Intel |
| `POST /api/intel/signals`, `GET /api/signals`, `PATCH /api/signals/{id}` | Buying signals |
| `POST /api/intel/source`, `GET /api/accounts?source=sourced`, `POST /api/leads/import-csv` | Lead sourcing and contact import |
| `POST /api/deals/{id}/review`, `POST /api/deals/review-all`, `GET /api/deals/{id}/strategy` | Deal Strategist |
| `GET /api/forecast/numbers`, `POST /api/forecast`, `GET /api/forecast/latest` | Forecast (numbers are deterministic; the agent writes the narrative) |
| `GET /api/insights/attribution`, `GET /api/insights/ab-tests` | Last-touch attribution and A/B results |
| `POST /api/insights/learn`, `GET /api/insights/playbook` | Learning loop |
| `POST /api/integrations/slack/events`, `GET/POST/DELETE /api/integrations/slack/channels` | Slack ingestion and channel opt-in |
| `POST /api/integrations/zoom`, `POST /api/integrations/transcripts` | Call transcripts → Call Analyst |
| `POST /api/integrations/calendar-events`, `GET /api/integrations/meetings` | Calendar → automatic meeting prep |
| `GET /api/accounts/{id}/timeline`, `POST /api/accounts/{id}/notes` | Customer timeline and rep notes |
| `GET /api/integrations/unmatched`, `POST /api/integrations/events/{id}/assign`, `GET /api/integrations/status` | Unmatched calls/messages, setup status |
| `GET /api/integrations/oauth/{google\|microsoft}/start`, `.../callback`, `GET/DELETE /api/integrations/connections`, `POST /api/integrations/sync` | Connected mailboxes and calendars |
| `POST /api/integrations/slack/interactions`, `POST /api/integrations/slack/commands` | Slack approval buttons and `/revenueos` |
| `POST /api/leads/{id}/dossier`, `POST /api/accounts/{id}/committee` | Person dossier; buying-committee outreach |
| `GET /api/insights/lift`, `POST /api/leads/{id}/release-holdout` | Holdout lift report |
| `POST /api/auth/bootstrap`, `/api/auth/login`, `/api/auth/logout`, `GET /api/auth/me` | Sign-in |
| `GET/POST /api/workspaces` (owner), `GET/PUT /api/workspace/settings`, `GET/POST/DELETE /api/workspace/users` (admin) | Clients, settings, people |
| `GET /api/onboarding/status`, `POST /api/onboarding/test-crm`, `/knowledge`, `/pilot` | Setup checklist |
| `POST/GET /api/evaluations`, `GET /api/evaluations/{id}`, `POST .../items/{item}/rate`, `GET .../results` | Blind test |
| `GET /api/insights/roi`, `GET /api/pilot/report`, `GET /api/pilot/report.md` | ROI and pilot report |
| `GET /api/analytics` | Dashboard numbers |
| `POST /api/crm/import` | Pull open leads from Salesforce |

Interactive docs: http://localhost:8000/docs

## Before production
- Set `API_TOKEN` and `WEBHOOK_SECRET`. `API_TOKEN` is a single shared token exposed to the browser (`NEXT_PUBLIC_API_TOKEN`), which is fine for an internal pilot. Put SSO in front for wider use.
- Use Postgres (`DATABASE_URL`). Docker Compose already does this.
- New columns are added to existing tables automatically on startup (additive only). Before larger schema changes in production, adopt Alembic.
- Generated .pptx/.docx files go to `GENERATED_DIR` (default `backend/generated`). Mount a volume there in Docker.
- Set `BRAND_COLOR` (hex) for decks and proposals, or load your own templates in `tools/docgen.py`.
- Send from a warmed-up domain with SPF, DKIM and DMARC configured.
- Have marketing or legal approve every case study in the knowledge base.

## Running more than one server process

Safe by design: each workspace's scheduled work runs only in the process holding its scheduler lease, and every send first claims the draft with one conditional update, so a follow-up can never go out twice. If a process dies mid-send, the draft is marked **failed** for a person to check (never resent automatically). Runs stuck as "running" for over two hours are marked failed.

## Database migrations

Schema changes beyond new nullable columns go in Alembic revisions (`backend/migrations/versions`). Every workspace database is upgraded on startup; existing databases are stamped at the baseline first.

```bash
cd backend
python -m revenueos.migrations revision -m "describe the change"   # autogenerate against the default DB
python -m revenueos.migrations upgrade                            # all workspaces
python -m revenueos.migrations current
```

## Tests

```bash
cd backend
python -m pytest -q
```

The tests cover KB retrieval, guardrails (including unverified figures), the approve→send flow, suppression, reply triage (out-of-office resume, referrals), call analysis → deal, proposal file generation and download, hygiene and merging, campaign audience targeting, content and ABM queueing, invented-figure flags on marketing copy, battlecard KB refresh, signal filtering and de-duplication, sourcing de-duplication, CSV import, the API, and the agent runtime with a mocked Claude client.

## What's next
- **Data:** pgvector embeddings for knowledge-base search once it grows beyond a few thousand chunks; multi-touch attribution once there is enough volume.
- **Integrations:** call recordings from Gong, Zoom or Teams for the Call Analyst; calendar booking from interested replies; an enrichment API (Apollo/ZoomInfo) in place of CSV import.
