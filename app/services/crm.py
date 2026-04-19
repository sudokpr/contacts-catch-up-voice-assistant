"""
Mock CRM adapter — simulates deal closure notifications from a CRM like Salesforce.

In production, replace `DEAL_FIXTURES` with a real Salesforce/HubSpot webhook listener
or polling integration. The fixture keys are lowercase contact names.
"""

import logging
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

# Fixture: deals closed with us today, keyed by lowercase contact name.
# Set "closed_today": True for contacts whose deal should trigger a call in the demo.
DEAL_FIXTURES: dict[str, dict] = {
    "arjun mehta": {
        "deal_name": "RelayAI Pro Annual License",
        "amount": "₹2.4L",
        "closed_today": True,
    },
}


def get_closed_deal_today(contact_name: str) -> dict | None:
    """
    Return deal metadata if this contact closed a deal with us today, else None.
    In production this would query Salesforce/HubSpot for deals closed on today's date.
    """
    fixture = DEAL_FIXTURES.get(contact_name.lower())
    if fixture and fixture.get("closed_today"):
        logger.info(
            "CRM: deal '%s' (%s) closed today for contact '%s'",
            fixture.get("deal_name"),
            fixture.get("amount"),
            contact_name,
        )
        return fixture
    return None
