"""No external CRM: RevenueOS's own database is the system of record."""
import logging

from ...models import Contact
from .base import CRMAdapter

log = logging.getLogger(__name__)


class LocalCRM(CRMAdapter):
    name = "local"

    def log_activity(self, contact: Contact, subject: str, description: str) -> str | None:
        log.info("local CRM activity for %s: %s", contact.email, subject)
        return None
