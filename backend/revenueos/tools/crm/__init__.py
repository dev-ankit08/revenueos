"""CRM adapters. RevenueOS sits on top of whichever CRM is the system of record (CRM_PROVIDER),
chosen per workspace, so each client can use a different CRM."""
from ...config import settings
from ...db import current_workspace
from .base import CRMAdapter
from .local import LocalCRM

CRM = CRMAdapter  # the contract every adapter implements

_crm: dict[int | None, CRMAdapter] = {}


def make_crm(provider: str) -> CRMAdapter:
    if provider == "salesforce":
        from .salesforce import SalesforceCRM
        return SalesforceCRM()
    if provider == "hubspot":
        from .hubspot import HubSpotCRM
        return HubSpotCRM()
    if provider == "dynamics":
        from .dynamics import DynamicsCRM
        return DynamicsCRM()
    if provider == "zoho":
        from .zoho import ZohoCRM
        return ZohoCRM()
    if provider == "pipedrive":
        from .pipedrive import PipedriveCRM
        return PipedriveCRM()
    if provider == "local":
        return LocalCRM()
    raise ValueError(f"Unknown CRM_PROVIDER {provider!r}: use local, salesforce, hubspot, dynamics, zoho or pipedrive.")


def get_crm() -> CRMAdapter:
    ws = current_workspace.get()
    if ws not in _crm:
        _crm[ws] = make_crm(settings.crm)
    return _crm[ws]


def forget(ws: int | None) -> None:
    """Drop the cached adapter after a workspace's CRM settings change."""
    _crm.pop(ws, None)
