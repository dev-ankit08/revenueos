// HubSpot UI extension (app card on the contact record). Opens the RevenueOS panel in a modal.
// Add to a HubSpot developer project's app card; set REVENUEOS_URL to your deployment, and add
// that origin to the app's permitted URLs for iframes. Check field names against your HubSpot
// project platform version before deploying.
import React from "react";
import { Button, Text, hubspot } from "@hubspot/ui-extensions";

const REVENUEOS_URL = "https://revenueos.yourcompany.com";

hubspot.extend(({ context, actions }) => <RevenueOSCard context={context} actions={actions} />);

const RevenueOSCard = ({ context, actions }) => {
  const recordId = context.crm.objectId;
  const open = () =>
    actions.openIframeModal({
      uri: `${REVENUEOS_URL}/embed?crm=hubspot&id=${encodeURIComponent(recordId)}`,
      height: 900,
      width: 720,
      title: "RevenueOS",
    });
  return (
    <>
      <Text>Research, personalised drafts, call history and approvals for this contact.</Text>
      <Button onClick={open}>Open RevenueOS</Button>
    </>
  );
};
