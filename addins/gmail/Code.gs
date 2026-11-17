/**
 * RevenueOS for Gmail (Google Workspace add-on).
 * Setup: script.google.com > New project > paste this file and appsscript.json > Project Settings >
 * Script properties: APP_URL = your APP_BASE_URL (e.g. https://revenueos.example.com) > Deploy > Test deployments
 * (or publish to your domain in the Google Workspace Marketplace SDK).
 */
function onGmailMessage(e) {
  var appUrl = PropertiesService.getScriptProperties().getProperty('APP_URL') || 'https://revenueos.example.com';
  GmailApp.setCurrentMessageAccessToken(e.gmail.accessToken);
  var message = GmailApp.getMessageById(e.gmail.messageId);
  var from = message.getFrom();
  var match = from.match(/<([^>]+)>/);
  var email = (match ? match[1] : from).trim();

  var section = CardService.newCardSection()
    .addWidget(CardService.newDecoratedText().setTopLabel('Prospect').setText(email))
    .addWidget(CardService.newTextParagraph().setText(
      'Open this person in RevenueOS: research, the customer timeline and any drafts waiting for approval.'))
    .addWidget(CardService.newTextButton()
      .setText('Open in RevenueOS')
      .setOpenLink(CardService.newOpenLink()
        .setUrl(appUrl + '/embed?email=' + encodeURIComponent(email))
        .setOpenAs(CardService.OpenAs.OVERLAY)));
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('RevenueOS'))
    .addSection(section)
    .build();
}
