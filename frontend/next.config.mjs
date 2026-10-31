/** @type {import('next').NextConfig} */
// Which CRM domains may show the RevenueOS side panel (/embed) in an iframe.
const embedOrigins = process.env.EMBED_ALLOWED_ORIGINS ??
  "'self' https://*.lightning.force.com https://*.my.salesforce.com https://app.hubspot.com https://app-eu1.hubspot.com https://*.dynamics.com";

const nextConfig = {
  output: "standalone",
  async headers() {
    return [
      { source: "/embed", headers: [{ key: "Content-Security-Policy", value: `frame-ancestors ${embedOrigins}` }] },
      { source: "/((?!embed).*)", headers: [{ key: "Content-Security-Policy", value: "frame-ancestors 'self'" }] },
    ];
  },
};
export default nextConfig;
