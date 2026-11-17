# RevenueOS in Outlook and Gmail

Reps see a prospect's research, customer timeline and waiting drafts beside the email they are reading, and can
approve drafts without switching apps. Both add-ins open the `/embed` panel (the same one used inside the CRM).

| Add-in | Folder | Install |
|---|---|---|
| Outlook (desktop, web, Mac) | `outlook/manifest.xml` | Replace `https://revenueos.example.com` with your `APP_BASE_URL`, then Outlook → Get Add-ins → My add-ins → Add a custom add-in → Add from file. For the whole company, deploy it in the Microsoft 365 admin center (Integrated apps). |
| Gmail | `gmail/Code.gs`, `gmail/appsscript.json` | Create an Apps Script project, paste both files, set the script property `APP_URL`, then Deploy → Test deployments (or publish to your domain through the Google Workspace Marketplace SDK). |

Notes:
- Add your app domain to `EMBED_ALLOWED_ORIGINS` if Outlook on the web refuses to frame the panel
  (for example `https://outlook.office.com https://outlook.office365.com`).
- The panel uses the rep's RevenueOS sign-in. On first use they may be asked to sign in once inside the panel.
- Icons: put `icon-64.png` and `icon-128.png` in `frontend/public` (an SVG icon is included for the web app).
