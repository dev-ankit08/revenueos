# RevenueOS: features that fix what customers struggle with (stand-out plan)

Target: mid-market B2B (50–1,000 staff, 3–30 reps), global English-first, buyer-facing features in scope.

## Context: what the research says customers struggle with

Sources are mostly vendor blogs and secondary reports; exact figures are directional, but the themes repeat across
independent sources.

| Struggle | Evidence | RevenueOS today |
|---|---|---|
| **Deliverability / burned domains** is the #1 complaint about AI outreach tools; category churn 50–70%/yr | Gmail/Yahoo/Microsoft require SPF+DKIM+DMARC and <0.3% spam complaints (aim <0.1%); safe cold volume ~20–50 emails per mailbox per day; warm new domains 14–21 days | One sender address, no warm-up, no mailbox rotation, no DNS or placement checks |
| **Reps don't trust or adopt it**: "too automated, couldn't tweak", robotic follow-ups, hallucinations; 70% of sellers overwhelmed by tools; tools die when reps must switch apps | AI SDR reviews; Gartner via vendor blogs | Approvals + edit/reject exist; but reps must open RevenueOS; drafts don't sound like the rep |
| **Admin burden**: reps sell 30–40% of the week; 71% spend 5+ h/week on CRM data entry | Salesforce State of Sales, SPOTIO | Call analysis and sends update the CRM; nothing for the rest of a rep's activity |
| **Forecasts miss**: 4 in 5 missed a quarterly forecast; median accuracy 70–79% | Xactly, Gartner (secondhand) | Forecast numbers + narrative; no tracking of forecast vs actual, no slip prediction |
| **Meetings don't become pipeline**; no-shows | AI SDR post-mortems | Meetings prepped; outcome not tracked |
| **Marketing can't prove ROI** (#1 challenge, 68%); attribution across channels and buying groups | CMI 2026, Anteriad | Multi-touch attribution, lift, ROI exist; no board-ready statement |
| **Buyers want self-serve** (67% rep-free preference); buying groups 5–16 people; deals with a shared plan report ~26% higher win rate | Gartner, Forrester, Mindtickle/Trumpet (vendor) | Committee outreach exists; nothing the buyer can see or use |
| **Heavy setup** (Agentforce needs weeks; KB must be hand-written) | Agentforce reviews | Setup checklist + manual KB |
| **Job changes / warm paths**: champions move, bounces hide it | UserGems pattern | Bounce → "may have left" only |

## The twelve features, in three phases

Principle: every feature either removes a reason customers churn, removes work from a rep's day, or gives a
leader proof. Nothing sends to a customer without a person approving it.

### Phase A (weeks 1–4): trusted delivery and daily adoption — the churn killers

**1. Deliverability Guardian** (biggest churn driver in the category)
- Mailbox pool: new `SendingMailbox` (address, provider, daily cap default 40, warm-up stage, health, paused). Round-robin
  sends across healthy mailboxes; spread sends across the send window instead of bursts
  (`services/sequences.py: next_send_time`, `services/approvals.py: claim_and_send`, `tools/email.py`).
- Warm-up ramp: a new mailbox starts at 5/day and grows over 14–21 days; connected Gmail/Outlook mailboxes
  (`integrations/mail.py`) can send as well as read.
- DNS readiness check on Integrations: SPF, DKIM, DMARC, reverse DNS (new dependency `dnspython`), with plain-English fixes
  and a 0–100 deliverability score.
- Health auto-pause: per-mailbox bounce and complaint rates from `EmailEvent` (`services/email_events.py`); pause a mailbox at
  >0.1% complaints or >2% bounces and alert the owner (`services/notify.py: alert`).
- Inbox placement test: send to a seed list of the team's own Gmail/Outlook addresses and read back inbox vs spam through
  the connected mailboxes. Shown as "placement 80%+ healthy".
- Settings: `COLD_DOMAIN` warning when the sending domain equals the company's main domain.

**2. Work where reps already are** (adoption)
- Approve by email: a digest of pending drafts with signed Approve / Reject links and reply-to-approve
  (sign with `services/secrets.sign`, same pattern as `services/privacy.unsubscribe_token`; reuse `notify.send_digests`).
- Outlook add-in and Gmail add-on: thin manifests that open `/embed?email=<sender>` beside the open email
  (`frontend/app/embed/page.tsx` already supports `?email=`); folder `addins/` next to `crm-panels/`.
- Installable mobile app (PWA manifest + icons) for My day, Tasks and Approvals; one-tap approve.

**3. Rep Voice: drafts that sound like the rep** (trust, "robotic" complaint)
- With the rep's consent, build a style profile from 30–50 of their own sent emails already in the timeline
  (`ContextEvent` source email, author = rep): length, greeting, sign-off, favourite phrases, formality. Haiku summarises;
  stored per user (`services/voice.py`, new `User.style_profile`).
- Injected into the writer and reply writer as a per-owner addendum (same mechanism as `governance.prompt_addendum`).
- Grader gets a "sounds like <rep>" check; Insights shows edit size before/after Rep Voice per rep.

**4. Follow-up value check** (robotic follow-ups)
- New guardrail in `services/guardrails.py`: a follow-up that repeats >40% of an earlier step (shingle overlap, reuse
  `_shingles`) or uses "just checking in / bumping this" phrases is flagged; the writer gets one rewrite
  (`agents/writer.py: make_queue_tool` rewrite loop).

### Phase B (weeks 4–8): zero admin and fewer surprises

**5. Zero-entry CRM**
- Activity capture: every rep-sent email and customer meeting already synced (`integrations/mail.py`, `calendar.py`) is logged
  to the CRM (`crm.log_activity`) and updates `last_activity_at`; contacts found in rep email threads are suggested as new
  contacts (one-click add, never auto-created).
- Quick update: after a meeting, the rep speaks or types two lines on mobile (browser speech-to-text, no new vendor);
  a light "update from note" path reuses the Call Analyst (`agents/deals.py: analyze_call`) to set stage, amount, next
  steps and tasks, then syncs the CRM. Target: a deal update in under 30 seconds.

**6. Forecast calibration and slip prediction**
- Weekly `ForecastSnapshot` (commit/best case/weighted, per-deal probability, rep's own call). At quarter end compare to
  actuals: accuracy by rep, by AI, by stage; shown on Insights (extend `services/insights.py: forecast_numbers`).
- Slip predictor (rules, no AI cost): close date pushed ≥2 times, no activity 14 days, no next step, single-threaded,
  champion silent → "likely to slip" flag, a task for the owner and a line in the Forecast Analyst's narrative
  (`agents/strategy.py`).

**7. Meeting-to-pipeline loop**
- Link each customer meeting (`CalendarEvent`) to its deal and outcome (held / no-show / qualified / next step booked).
- No-show reduction: reminder email 24h before (through the sequence engine; approved template, like nurture).
- Dashboard: meetings → qualified → opportunity → won, by source and by assistant; the number AI SDR buyers say is missing.

**8. Relationship graph and job-change signals**
- Who knows whom: from connected mailboxes and calendars (metadata only), strength = count and recency; on an account page:
  "Priya has met Ankit twice" → warm intro suggestion in committee outreach.
- Job changes: quarterly re-check of key contacts through the enrichment adapter (`services/datahealth.py`) plus bounces;
  "champion moved to <company>" creates a signal and a suggested lead at the new company (`services/growth._signal_once`).

### Phase C (weeks 8–12): buyer-facing and proof

**9. Buyer Room** (self-serve buyers, buying groups, win rate)
- A private page per deal (signed link, no login): tailored summary, proposal/deck (`tools/docgen.py` output), matching case
  studies from the KB, and a **mutual action plan** (steps, owners on both sides, dates; buyer can tick and comment).
- Buyer Q&A assistant grounded only in the KB and the deal's documents, with the same guardrails and rule packs
  (no pricing unless allowed, claims checked); unanswerable questions become a task for the rep.
- Engagement: who opened what and for how long (reuse the `services/growth.record_visit` pattern) feeds deal health and
  shows buying-group coverage gaps (roles never seen in the room).
- New models `BuyerRoom`, `RoomStep`, `RoomView`; `api/rooms.py`; public page `frontend/app/room/[token]/page.tsx`.

**10. Buying-group coverage score**
- Per account: required roles (from `agents/committee.py` missing roles) vs roles engaged (replies, meetings, room views).
  "Single-threaded" becomes a deal risk and a Deal Strategist input.

**11. ROI Statement** (proving value, the #1 marketing challenge)
- One board-ready page: hours saved (per-run assumptions editable), meetings, pipeline, won revenue, AI spend, holdout lift,
  forecast accuracy, deliverability health, quality by assistant. Export to PDF/Word (`tools/docgen.py`); extend
  `api/insights.py: _pilot` and the pilot report. Email it monthly to leaders (`notify.send_internal`).

**12. Onboarding in a day** (heavy-setup pain)
- KB auto-build: a `kb_builder` agent reads the company website and uploaded PDFs (web_fetch + `tools/kb.add_document`) and
  drafts offering, ICP, personas, brand voice and case studies for approval, each with its source page.
- Import sequences and contacts from Outreach/Salesloft/HubSpot CSV exports (extend `api/leads.py: import_csv`).
- "First value in 60 minutes" checklist on Onboarding: connect mailbox → DNS check → KB from website → 10 leads → first drafts.

### Later options (not in this plan)
- India/APAC pack: WhatsApp Business API templates, AI voice calling via a partner, Hindi/regional languages, TRAI DND checks.
- Per-contact language for drafts (cheap; add when a non-English customer appears).
- Real-time call coaching (Gong-style); LinkedIn automation stays copy-and-send (automation risks account restriction).

## Reuse map (don't rebuild)

- Sending and sequences: `services/approvals.py`, `services/sequences.py`, `tools/email.py`, `services/email_events.py`
- Mailbox/calendar sync: `integrations/mail.py`, `integrations/calendar.py`, `integrations/oauth.py`
- Guardrails and grading: `services/guardrails.py`, `services/personalisation.py`, `agents/writer.py: make_queue_tool`
- Signals, timeline, notifications: `services/growth.py`, `services/memory.py`, `services/notify.py`
- Insights and reports: `services/insights.py`, `api/insights.py: _pilot`, `tools/docgen.py`
- Signed links: `services/secrets.sign`, `services/privacy.unsubscribe_token`
- Public pages pattern: `api/privacy.py` (unsubscribe), `frontend/app/embed/page.tsx`

## Verification

1. Unit tests per feature with providers mocked at `integrations.http.request` (existing pattern); full suite stays green
   (`cd backend && python -m pytest -q`); frontend `npx tsc --noEmit`.
2. Deliverability: with two connected mailboxes, 60 approved emails spread across both within caps; a simulated complaint
   webhook pauses one mailbox and alerts; DNS check reports a missing DMARC record on a test domain; placement test marks
   a seed address that lands in spam.
3. Adoption: approve a draft from the email digest link; open the Outlook add-in on a prospect email and see their timeline;
   install the PWA on a phone and approve from My day.
4. Rep Voice: after profiling 30 sent emails, drafts for that rep use their sign-off and length; Insights shows lower edit size.
5. Admin: send an email from a connected mailbox → it appears on the CRM record; a spoken "quick update" sets stage and
   creates tasks.
6. Forecast: snapshots accumulate weekly; at quarter end accuracy per rep/AI is computed on test data; a pushed close date
   plus 14 idle days flags "likely to slip".
7. Buyer Room: open the signed link without login, tick a plan step, ask a pricing question (refused per rule pack),
   view the proposal; the deal's health and coverage update; the rep sees who viewed what.
8. ROI Statement exports to PDF with the numbers from Insights; KB auto-build drafts four documents from a test website
   and each cites its source page.

Success metrics to track from day one: inbox placement ≥80% and complaints <0.1%; weekly active reps and share of approvals
done from email/add-in/mobile; drafts approved unedited ≥70%; CRM fields updated without typing; forecast accuracy by quarter;
meetings → opportunities %; win rate with vs without a Buyer Room.

## Sources (research, 2026)

- AI SDR complaints and churn: https://www.oliv.ai/blog/ai-sdr-tools , https://salesmeup.de/blog/ai-in-sales-2026-8-reasons-ai-sdrs-fail/ ,
  https://b2bsignals.ai/blog/why-ai-sdrs-are-failing , https://www.apollo.io/insights/what-are-the-limitations-of-current-ai-sdr-tools
- Deliverability rules: https://www.miraoutbound.com/blog/cold-email-deliverability-guide-2026 , https://promattic.com/blog/cold-email-deliverability-2026/ ,
  https://growthengineer.ai/blog/cold-email-deliverability-2026
- Rep time and CRM entry: https://spotio.com/blog/state-of-field-sales-2026/ , https://www.landbase.com/blog/sales-reps-30-percent-time-selling-2026
- Forecast accuracy: https://amworldgroup.com/statistics/sales-forecasting-statistics , https://www.spotlight.ai/post/sales-forecasting-broken-2026
- Marketing challenges: https://contentmarketinginstitute.com/b2b-research/b2b-content-marketing-trends-research-2026 , https://anteriad.com/2026-b2b-marketing-edge
- Buyers and buying groups: https://www.omnibound.ai/blog/b2b-buying-statistics , https://ivristech.com/b2b-buying-group-statistics/
- Digital sales rooms / mutual action plans: https://www.mindtickle.com/blog/how-digital-sales-rooms-help-reps-win-more-deals/ ,
  https://www.sendtrumpet.com/blog-posts/do-digital-sales-rooms-actually-work
- Tool adoption: https://www.coevera.com/blog/why-sales-teams-dont-use-crm/ , https://www.vivun.com/blog/why-sales-ai-isnt-working-for-you-and-how-to-fix-it ,
  https://prospeo.io/s/sales-technology-adoption
- LinkedIn automation risk: https://www.zeliq.com/blog/linkedin-automation-2026 , https://snov.io/blog/linkedin-jail/
- Competitor direction: https://marketbetter.ai/blog/salesloft-vs-outreach-sdr-teams/ , https://buzzlead.io/blogs/ai-sales-agents-comparison-apollo-clay-salesloft-and-outreach-2026
