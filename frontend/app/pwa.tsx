"use client";
import { useEffect } from "react";

/** Registers the service worker so RevenueOS can be installed on a phone or desktop. */
export default function Pwa() {
  useEffect(() => {
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  }, []);
  return null;
}
