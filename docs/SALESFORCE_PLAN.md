# RevenueOS for Salesforce: complement Agentforce, don't compete with it

## Context

Salesforce now ships native agents (Agentforce SDR / Sales Development, Sales Coach, Prospecting) and HubSpot
prices its Prospecting Agent at $1 per qualified lead. A generic "AI SDR" competes head-on with the CRM itself
and with a category reporting 50-70% annual churn. The user wants RevenueOS to stand out by **complementing**
Salesforce.

What the research says Agentforce is weak at (2026, mostly third-party sources; verify with Salesforce docs):
- **Knowledge outside Salesforce** (Slack, call recordings, email, docs, the web) needs extra integration layers.
- **Output quality depends on CRM data quality**; duplicates and gaps produce weak agent output.
- **Hard to prove value and hard to budget**: consumption pricing, little independent measurement.
- **Outbound autonomy** for the Sales Development agent was still in pilot as of Sept 2026; setup is heavy.
- **Openness**: Agentforce can now call external tools as MCP servers (beta) and Apex/Flow/External Service actions.

What RevenueOS already has that Agentforce doesn't: a cross-system customer timeline (Slack, Gong, Zoom, Teams,
Gmail/Outlook, web research), claim verification against sources, a personalisation grader, human approval with
edit/reject learning, holdout lift tests, blind tests, multi-CRM and multi-client workspaces.

What blocks it today (code review): Salesforce access is one integration user with a password
(`backend/revenueos/tools/crm/salesforce.py`), it writes only standard fields (MEDDICC as text in Description),
pulls on a 15-minute poll, the Lightning panel is an unauthenticated iframe that doesn't work on Opportunity pages
(`crm-panels/salesforce/.../revenueosPanel`), and the MCP tools (`backend/revenueos/mcp_server.py`) take RevenueOS
ids, not Salesforce ids, with a single shared token.

## Positioning (one line)

**"The trust and intelligence layer for Agentforce."** Agentforce acts inside Salesforce; RevenueOS feeds it
everything the customer said outside Salesforce, checks every AI-written message before a customer sees it,
gets the org's data ready, and proves which agent and which message actually created pipeline.

Four things to build, each tied to an Agentforce weakness:

| Agentforce weakness | RevenueOS add-on | Who buys it |
|---|---|---|
| Doesn't see knowledge outside Salesforce | **Context for Agentforce**: RevenueOS timeline, briefs and dossiers as Agentforce actions and on the record | Sales ops, AEs |
| Can't vouch for its own output | **AI message QA**: grader + claim checks + approvals for *any* AI draft, including Agentforce's | Marketing, legal, sales leaders |
| Garbage-in data | **Agentforce readiness**: org data-health scan, fixes, enrichment and verification written back | RevOps, Salesforce admins |
| Value is hard to prove / budget | **Proof**: holdout lift, cost per meeting across Agentforce and RevenueOS, quality by agent | CRO, CFO |

---

## Phase 1 (weeks 1-4): become a real Salesforce citizen (foundation for everything)

1. **OAuth instead of a password.** Connected App / External Client App with the OAuth web-server flow (admin
   clicks "Connect Salesforce" on Integrations) and JWT bearer for servers; per-workspace encrypted refresh tokens.
   Reuse `services/secrets.py` (encrypt/rotate) and the OAuth pattern in `integrations/oauth.py`.
   File: `tools/crm/salesforce.py` (auth), `api/integrations.py` (connect/callback), `config.py`.
2. **Write results back as Salesforce data** (unlocked package in `crm-panels/salesforce/force-app`):
   - Custom fields: Lead/Contact `RevenueOS_Score__c`, `RevenueOS_Tier__c`, `RevenueOS_Lifecycle__c`,
     `RevenueOS_Email_Status__c`; Opportunity MEDDICC fields (`Metrics__c`, `Economic_Buyer__c`, ...),
     `RevenueOS_Health__c`, `RevenueOS_Win_Probability__c`, `RevenueOS_Next_Best_Action__c`.
   - Custom objects: `RevenueOS_Brief__c` (account research with sources), `RevenueOS_Draft__c` (draft, score,
     guardrail flags, status) so approvals are visible and reportable in Salesforce.
   - `OpportunityContactRole` for buying-committee roles; `OpportunityLineItem` from deal lines.
   - Extend the adapter contract in `tools/crm/base.py` (new optional methods, Salesforce implements them; other
     CRMs keep working). Composite API + External Id upserts to stay within API limits.
3. **Instant triggers instead of polling.** Change Data Capture / Pub/Sub API subscriber (or a Flow template that
   calls RevenueOS) for: new Lead/Contact, Opportunity stage change, new EmailMessage/Task. Keep the 15-minute
   pull as a safety net (`integrations/sync.py`).
4. **Salesforce ids everywhere.** Lookups and actions accept Salesforce record ids (Lead, Contact, Account,
   Opportunity). Fix the panel for Opportunity and Account pages (`api/leads.py: lookup`).
5. **Identity and permissions.** Map Salesforce users to RevenueOS users (owner = Salesforce OwnerId); sign-in with
   Salesforce as the identity provider using the existing OIDC support (`services/security.py`); the embedded panel
   gets a signed context (Canvas signed request or a short-lived token minted by Apex), and the approver is the
   real Salesforce user (reuse `api/approvals.py: reviewer_name`).

## Phase 2 (weeks 4-8): Agentforce actions pack ("Context for Agentforce")

1. **RevenueOS as an Agentforce tool server.** Extend `mcp_server.py` with OAuth-protected, Salesforce-id-based tools
   that Agentforce can import from API Catalog (MCP, beta) and, for orgs without the beta, an OpenAPI spec for
   External Services plus Apex `@InvocableMethod` wrappers with a Named Credential:
   - `get_customer_context(recordId)`: timeline, commitments, objections, stakeholders (from `services/memory.py`).
   - `get_account_brief(recordId)` / `research_account(recordId)` (async: returns at once, writes the brief to
     `RevenueOS_Brief__c` when done, so Agentforce never hits a timeout).
   - `check_message(text, recordId)`: guardrails + claim verification + personalisation score
     (reuse `services/guardrails.py: check_draft, check_claims`, `services/personalisation.py: grade`).
   - `draft_with_guardrails(recordId, goal)`: RevenueOS writer, queued for approval.
2. **Prompt Builder grounding.** A documented pattern (and a sample prompt template in the package) that pulls
   `get_customer_context` into Agentforce prompts, so Salesforce's own agents speak with full context.
3. **Native Lightning components** (replace the iframe): Customer timeline, Brief, Drafts-to-approve and Deal
   strategy as LWCs calling RevenueOS through the Named Credential.

## Phase 3 (weeks 8-12): the unique part (QA, readiness, proof)

1. **AI message QA for any agent.** Ingest outbound AI emails from Salesforce (EmailMessage / Task via CDC, tagged by
   sender: Agentforce, RevenueOS, rep) and run the same checks: invented figures, unverifiable claims, sensitive
   details, banned phrases, personalisation score. Optionally route Agentforce drafts through RevenueOS approvals.
   Results on the record and in a "Message quality by agent" dashboard (extend `services/insights.py: draft_quality`).
2. **Agentforce readiness scan.** One click: duplicates, missing fields the agents depend on, stale or bounced
   contacts, unverified emails, empty knowledge, with a 0-100 readiness score and one-click fixes written back to
   Salesforce (reuse `services/hygiene.py`, `services/datahealth.py`, enrichment and verification adapters).
3. **Proof across agents.** Holdout lift and cost per meeting for Agentforce outreach as well as RevenueOS
   outreach (extend `services/insights.py: lift_report, roi_ledger`, add Agentforce credit cost input), so leaders
   can see which agent and which message creates pipeline per dollar.

## Packaging and go-to-market (in parallel)

- **Unlocked package first** (for CriticalRiver clients and pilots), managed package + AppExchange security review
  once 3-5 orgs use it. Package contents: custom fields/objects, permission sets, LWCs, Named/External Credential,
  Apex action wrappers, Flow templates, prompt template.
- **Lead offer for CriticalRiver's Salesforce practice:** "Agentforce Readiness + Trust Layer" engagement:
  readiness scan, data fixes, Agentforce + RevenueOS setup, then a monthly subscription for QA, context and proof.
- **Dev org for testing:** a Salesforce Developer Edition org with Agentforce enabled (free) plus a sandbox from a
  friendly client for the pilot.

## Critical files

- `backend/revenueos/tools/crm/salesforce.py`, `tools/crm/base.py`: OAuth, write-back, Composite/External Id upserts
- `backend/revenueos/integrations/sync.py`, new `integrations/salesforce_events.py`: CDC / Pub/Sub subscriber
- `backend/revenueos/mcp_server.py`, new `api/agentforce.py`: Salesforce-id tools, OAuth, async callbacks, OpenAPI
- `backend/revenueos/api/leads.py` (lookup by any Salesforce id), `api/approvals.py` (Salesforce user as reviewer)
- `backend/revenueos/services/insights.py`: quality by agent, cross-agent lift and cost per meeting
- `crm-panels/salesforce/force-app/`: custom fields/objects, permission sets, LWCs, Apex invocables, Named Credential,
  Flow templates, prompt template

## Verification

1. Unit tests for every new adapter method and action with Salesforce HTTP mocked at `integrations.http.request`
   (same pattern as the existing CRM contract tests); full suite stays green.
2. In a free Developer Edition org: install the unlocked package, connect via OAuth, then check:
   - Lead created in Salesforce appears in RevenueOS within seconds (CDC) and gets a score written to `RevenueOS_Score__c`.
   - Research writes a `RevenueOS_Brief__c` record; call analysis fills MEDDICC fields and contact roles.
   - In Agentforce Builder, import the RevenueOS tools; an agent on an Account answers with timeline context and
     `check_message` flags an invented figure in an Agentforce-written email.
   - Readiness scan shows a score and a fix updates records in Salesforce.
3. Pilot with one CriticalRiver client sandbox: compare message quality and meetings per 100 contacts for Agentforce
   alone vs Agentforce + RevenueOS (holdout).
