"use client";
import { useEffect, useState } from "react";

declare global { interface Window { Office?: any } }

/** Opened by the Outlook add-in: reads the sender of the open email and shows their RevenueOS panel. */
export default function OutlookAddin() {
  const [msg, setMsg] = useState("Loading…");

  useEffect(() => {
    const s = document.createElement("script");
    s.src = "https://appsforoffice.microsoft.com/lib/1/hosted/office.js";
    s.onload = () => window.Office?.onReady(() => {
      const from = window.Office?.context?.mailbox?.item?.from?.emailAddress;
      if (from) window.location.replace(`/embed?email=${encodeURIComponent(from)}`);
      else setMsg("Open an email to see the sender in RevenueOS.");
    });
    s.onerror = () => setMsg("Couldn't load Outlook's add-in library. Check your network and reopen the panel.");
    document.head.appendChild(s);
  }, []);

  return <p className="muted" style={{ padding: 16 }}>{msg}</p>;
}
