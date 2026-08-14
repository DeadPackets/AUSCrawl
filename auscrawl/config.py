"""Endpoints, tuning constants, and the browser header profile."""

BASE = "https://register.aus.edu/StudentRegistrationSsb/ssb"

EP = {
    "terms": f"{BASE}/classSearch/getTerms",
    "term_selection": f"{BASE}/term/termSelection",
    "term_search": f"{BASE}/term/search",
    "reset": f"{BASE}/classSearch/resetDataForm",
    "sections": f"{BASE}/searchResults/searchResults",
    "catalog": f"{BASE}/courseSearchResults/courseSearchResults",
    "ref_subject": f"{BASE}/classSearch/get_subject",
    "ref_instructor": f"{BASE}/classSearch/get_instructor",
    "ref_attribute": f"{BASE}/classSearch/get_attribute",
    "prereqs": f"{BASE}/courseSearchResults/getPrerequisites",
    "coreqs": f"{BASE}/courseSearchResults/getCorequisites",
    "restrictions": f"{BASE}/courseSearchResults/getRestrictions",
    "course_attributes": f"{BASE}/courseSearchResults/getCourseAttributes",
    "course_catalog_details": f"{BASE}/courseSearchResults/getCourseCatalogDetails",
}

# The server clamps pageMaxSize to 500; asking for more just wastes the round trip.
PAGE_SIZE = 500

DEFAULT_RATE = 10.0
MAX_RATE = 20.0
MIN_RATE = 2.0
SESSION_POOL_SIZE = 6
DETAIL_CONCURRENCY = 12
MAX_RETRIES = 5
RETRY_BASE = 2.0
DETAIL_BATCH_SIZE = 2000

RETRYABLE_STATUS = frozenset({403, 408, 429}) | frozenset(range(500, 600))
THROTTLE_STATUS = frozenset({429, 503})

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# A single coherent Chrome identity. Rotating user agents is itself a detection
# signal; a consistent, current one is not.
BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Referer": f"{BASE}/classSearch/classSearch",
}

_SEASON_ORDER = {"10": 0, "11": 1, "20": 2, "30": 3, "40": 4}


def term_name_to_sort_key(term_id: str) -> tuple[int, int]:
    """Chronological key for a Banner term id such as '202611'."""
    return (int(term_id[:4]), _SEASON_ORDER.get(term_id[4:], 9))
