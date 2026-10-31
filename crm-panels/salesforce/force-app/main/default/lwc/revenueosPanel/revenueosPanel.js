import { LightningElement, api } from "lwc";

// Shows the RevenueOS side panel on Lead, Contact and Opportunity record pages.
// baseUrl is set per page in Lightning App Builder (e.g. https://revenueos.yourcompany.com).
export default class RevenueosPanel extends LightningElement {
  @api recordId;
  @api baseUrl;

  get src() {
    const base = (this.baseUrl || "").replace(/\/$/, "");
    return `${base}/embed?crm=salesforce&id=${encodeURIComponent(this.recordId || "")}`;
  }
}
