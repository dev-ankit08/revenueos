import type { Metadata } from "next";
import Nav from "./nav";
import Pwa from "./pwa";
import "./globals.css";

export const metadata: Metadata = {
  title: "RevenueOS", description: "AI agents for sales and marketing",
  manifest: "/manifest.webmanifest", icons: { icon: "/icon.svg", apple: "/icon.svg" },
  appleWebApp: { capable: true, title: "RevenueOS", statusBarStyle: "default" },
};

export const viewport = { themeColor: "#3B5BDB", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Pwa />
        <div className="shell">
          <Nav />
          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}
