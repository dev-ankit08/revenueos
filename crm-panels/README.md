# RevenueOS side panel inside your CRM

The panel is one page, `/embed`, that shows the selected record's rating, research angle, person dossier,
drafts waiting for approval (with Approve / Reject) and the customer timeline. Each CRM shows it in a frame:

| CRM | File | How to install |
|---|---|---|
| Salesforce | `salesforce/force-app/main/default/lwc/revenueosPanel` | Deploy with `sf project deploy start -d force-app`. In Setup → **Trusted URLs**, add your RevenueOS web address with *frame-src* allowed. Drag **RevenueOS panel** onto the Lead/Contact/Opportunity page in Lightning App Builder and set its web address. |
| HubSpot | `hubspot/RevenueOSCard.jsx` | Add as an app card in a HubSpot developer project, set `REVENUEOS_URL`, deploy, and add the card to the contact record. |
| Dynamics 365 | `dynamics/revenueos_panel.html` | Upload as an HTML web resource, set `REVENUEOS_URL`, add it to the Lead form with "Pass record object-type code and unique identifier as parameters" ticked. |

**Before going live**
- The CRM's domain must be allowed to frame RevenueOS: `EMBED_ALLOWED_ORIGINS` on the web app (defaults cover
  Salesforce Lightning, HubSpot and `*.dynamics.com`).
- Records are matched by CRM id (`sf_id` for Salesforce, `crm_id` for HubSpot/Dynamics), falling back to email.
  Import open leads from the CRM first (`POST /api/crm/import` or the Pipeline page button).
- These are templates: they were written against each vendor's documented extension model but have not been
  deployed to a live org yet. Test in a sandbox first.
