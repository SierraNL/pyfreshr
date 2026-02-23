from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class ScrapeResult:
    """Structured result from a scrape.

    Attributes:
        url: The page URL scraped.
        data: A mapping of field names to extracted string values (or None).
    """
    url: str
    data: Dict[str, Optional[str]]
