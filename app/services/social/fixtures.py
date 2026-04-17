from datetime import datetime, UTC, timedelta

from app.models.contact import Contact
from app.services.social.base import SocialUpdate


def _ts(days_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


FIXTURES: dict[str, dict[str, list[SocialUpdate]]] = {
    "twitter": {
        "__default__": [
            SocialUpdate(platform="twitter", text="Shared a quick life update.", timestamp=_ts(2)),
        ],
        "maya patel": [
            SocialUpdate(platform="twitter", text="just merged 200 PRs worth of k8s migration. i need a vacation and possibly a therapist 🙃", timestamp=_ts(3)),
        ],
        "david okafor": [
            SocialUpdate(platform="twitter", text="Day 47 of the startup. Survived three investor rejections and one near-death pivot. Still standing.", timestamp=_ts(5)),
        ],
    },
    "instagram": {
        "__default__": [
            SocialUpdate(platform="instagram", text="Shared a weekend photo dump.", timestamp=_ts(3)),
        ],
        "sarah chen": [
            SocialUpdate(platform="instagram", text="Six months in London. Still pinching myself. 📚☕️ #expatlife", timestamp=_ts(7)),
        ],
    },
    "linkedin": {
        "__default__": [
            SocialUpdate(platform="linkedin", text="Reacted to an industry post.", timestamp=_ts(4)),
        ],
        "maya patel": [
            SocialUpdate(platform="linkedin", text="Thrilled to announce my promotion to Staff Engineer at CloudBase!", timestamp=_ts(10)),
        ],
        "priya menon": [
            SocialUpdate(platform="linkedin", text="Honoured to speak at the National Medical Conference in Bangalore last week.", timestamp=_ts(6)),
        ],
        # Business partners — deal-news and promotion fixtures
        "arjun mehta": [
            SocialUpdate(platform="linkedin", text="Excited to share that our startup secured ₹5 crore in seed funding! Grateful to our investors and team.", timestamp=_ts(0)),
        ],
        "priya sharma": [
            SocialUpdate(platform="linkedin", text="Proud moment — we closed a ₹10 crore logistics contract this quarter. Our team delivered!", timestamp=_ts(0)),
        ],
        "marcus weber": [
            SocialUpdate(platform="linkedin", text="Honoured to be promoted to Managing Partner at Weber Capital. Grateful for the team's trust.", timestamp=_ts(1)),
        ],
    },
}

# Keywords that indicate a major deal or funding announcement
DEAL_KEYWORDS = ["million", "crore", "deal", "funding", "raised", "secured", "contract", "investment", "closed"]
# Keywords that indicate a promotion or new senior role
PROMOTION_KEYWORDS = ["promoted", "promotion", "new role", "managing partner", "vp ", "director", "chief ", "head of"]


def get_fixture_updates(contact: Contact, platform: str) -> list[SocialUpdate]:
    platform_fixtures = FIXTURES.get(platform, {})
    return platform_fixtures.get(contact.name.lower(), platform_fixtures.get("__default__", []))
