"""Crawl 591 rent listing pages (server-side rendered) and return listings as JSON."""

import json
import logging
import re
import threading
import time
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    # Use the OS trust store (fixes CERTIFICATE_VERIFY_FAILED with some
    # Python/OpenSSL builds); harmless no-op if truststore is absent.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_URL = "https://rent.591.com.tw/list"
ALLOWED_DETAIL_HOST = "rent.591.com.tw"
LOGGER = logging.getLogger(__name__)
_THREAD_LOCAL = threading.local()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


class CrawlerParseError(RuntimeError):
    """Raised when a successful HTTP response is not a recognizable list page."""


def _http_session():
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
    return session


def _http_get(url, *, timeout):
    current = url
    for _ in range(4):
        parsed = urlparse(current)
        if (
            parsed.scheme != "https"
            or parsed.hostname != ALLOWED_DETAIL_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            raise ValueError(f"request URL must stay on https://{ALLOWED_DETAIL_HOST}")
        response = _http_session().get(
            current,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            continue
        return response
    raise requests.TooManyRedirects("too many redirects from 591")


def _validate_detail_url(url):
    if not isinstance(url, str):
        raise ValueError("detail URL must be a string")
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("detail URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_DETAIL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError(f"detail URL must use https://{ALLOWED_DETAIL_HOST}")
    if not re.fullmatch(r"/\d+/?", parsed.path):
        raise ValueError("detail URL path must contain a numeric listing id")
    return url

# Region (縣市) ids and per-region section (鄉鎮市區) ids, as used by the site's
# own frontend data. Section names are only unique within their region.
REGIONS = {
    1: "台北市",
    2: "基隆市",
    3: "新北市",
    4: "新竹市",
    5: "新竹縣",
    6: "桃園市",
    7: "苗栗縣",
    8: "台中市",
    10: "彰化縣",
    11: "南投縣",
    12: "嘉義市",
    13: "嘉義縣",
    14: "雲林縣",
    15: "台南市",
    17: "高雄市",
    19: "屏東縣",
    21: "宜蘭縣",
    22: "台東縣",
    23: "花蓮縣",
    24: "澎湖縣",
    25: "金門縣",
    26: "連江縣",
}

SECTIONS = {
    1: {5: "大安區", 10: "內湖區", 8: "士林區", 12: "文山區", 9: "北投區", 3: "中山區", 7: "信義區", 4: "松山區", 6: "萬華區", 1: "中正區", 2: "大同區", 11: "南港區"},
    2: {17: "安樂區", 14: "信義區", 19: "七堵區", 15: "中正區", 16: "中山區", 13: "仁愛區", 18: "暖暖區"},
    3: {26: "板橋區", 44: "新莊區", 38: "中和區", 43: "三重區", 34: "新店區", 39: "土城區", 37: "永和區", 27: "汐止區", 47: "蘆洲區", 50: "淡水區", 41: "樹林區", 46: "林口區", 40: "三峽區", 48: "五股區", 42: "鶯歌區", 45: "泰山區", 49: "八里區", 30: "瑞芳區", 28: "深坑區", 51: "三芝區", 20: "萬里區", 21: "金山區", 33: "貢寮區", 52: "石門區", 32: "雙溪區", 29: "石碇區", 35: "坪林區", 36: "烏來區", 31: "平溪區"},
    4: {371: "東區", 372: "北區", 370: "香山區"},
    5: {54: "竹北市", 61: "竹東鎮", 55: "湖口鄉", 56: "新豐鄉", 57: "新埔鎮", 58: "關西鎮", 59: "芎林鄉", 60: "寶山鄉", 63: "橫山鄉", 64: "尖石鄉", 65: "北埔鄉", 66: "峨嵋鄉", 62: "五峰鄉"},
    6: {73: "桃園區", 67: "中壢區", 68: "平鎮區", 75: "八德區", 70: "楊梅區", 74: "龜山區", 79: "蘆竹區", 69: "龍潭區", 76: "大溪區", 78: "大園區", 72: "觀音區", 71: "新屋區", 77: "復興區"},
    7: {81: "頭份市", 80: "竹南鎮", 88: "苗栗市", 87: "苑裡鎮", 85: "後龍鎮", 86: "通霄鎮", 91: "公館鄉", 94: "銅鑼鄉", 97: "卓蘭鎮", 95: "三義鄉", 92: "大湖鄉", 89: "造橋鄉", 90: "頭屋鄉", 83: "南庄鄉", 96: "西湖鄉", 82: "三灣鄉", 93: "泰安鄉", 84: "獅潭鄉"},
    8: {103: "北屯區", 104: "西屯區", 107: "大里區", 106: "太平區", 105: "南屯區", 110: "豐原區", 102: "北區", 100: "南區", 101: "西區", 116: "潭子區", 120: "沙鹿區", 117: "大雅區", 123: "清水區", 109: "烏日區", 121: "龍井區", 99: "東區", 124: "大甲區", 118: "神岡區", 108: "霧峰區", 122: "梧棲區", 119: "大肚區", 111: "后里區", 113: "東勢區", 125: "外埔區", 115: "新社區", 126: "大安區", 98: "中區", 112: "石岡區", 114: "和平區"},
    10: {127: "彰化市", 136: "員林市", 134: "和美鎮", 131: "鹿港鎮", 140: "溪湖鎮", 149: "二林鎮", 132: "福興鄉", 129: "花壇鄉", 137: "社頭鄉", 141: "大村鄉", 143: "田中鎮", 135: "伸港鄉", 130: "秀水鄉", 138: "永靖鄉", 139: "埔心鄉", 144: "北斗鎮", 151: "芳苑鄉", 142: "埔鹽鄉", 146: "埤頭鄉", 147: "溪州鄉", 145: "田尾鄉", 128: "芬園鄉", 133: "線西鄉", 150: "大城鄉", 148: "竹塘鄉", 152: "二水鄉"},
    11: {153: "南投市", 155: "草屯鎮", 157: "埔里鎮", 164: "竹山鎮", 159: "名間鄉", 156: "國姓鄉", 165: "鹿谷鄉", 161: "水里鄉", 158: "仁愛鄉", 163: "信義鄉", 162: "魚池鄉", 154: "中寮鄉", 160: "集集鎮"},
    12: {373: "西區", 374: "東區"},
    13: {180: "民雄鄉", 173: "水上鄉", 171: "中埔鄉", 176: "朴子市", 175: "太保市", 169: "竹崎鄉", 179: "新港鄉", 181: "大林鎮", 184: "布袋鎮", 177: "東石鄉", 178: "六腳鄉", 168: "梅山鄉", 183: "義竹鄉", 174: "鹿草鄉", 182: "溪口鄉", 167: "番路鄉", 170: "阿里山鄉", 172: "大埔鄉"},
    14: {194: "斗六市", 187: "虎尾鎮", 193: "麥寮鄉", 198: "西螺鎮", 185: "斗南鎮", 200: "北港鎮", 196: "古坑鄉", 188: "土庫鎮", 197: "莿桐鄉", 202: "口湖鄉", 199: "二崙鄉", 204: "元長鄉", 192: "崙背鄉", 201: "水林鄉", 191: "臺西鄉", 203: "四湖鄉", 186: "大埤鄉", 195: "林內鄉", 190: "東勢鄉", 189: "褒忠鄉"},
    15: {212: "永康區", 211: "安南區", 206: "東區", 209: "北區", 207: "南區", 208: "中西區", 219: "仁德區", 230: "新營區", 210: "安平區", 213: "歸仁區", 224: "佳里區", 238: "善化區", 223: "麻豆區", 214: "新化區", 241: "新市區", 220: "關廟區", 242: "安定區", 232: "白河區", 225: "西港區", 228: "學甲區", 237: "鹽水區", 235: "下營區", 231: "後壁區", 234: "六甲區", 226: "七股區", 222: "官田區", 236: "柳營區", 233: "東山區", 227: "將軍區", 216: "玉井區", 229: "北門區", 239: "大內區", 217: "楠西區", 218: "南化區", 240: "山上區", 215: "左鎮區", 221: "龍崎區"},
    17: {268: "鳳山區", 250: "三民區", 253: "左營區", 251: "楠梓區", 249: "前鎮區", 245: "苓雅區", 252: "小港區", 247: "鼓山區", 269: "大寮區", 254: "仁武區", 258: "岡山區", 270: "林園區", 259: "路竹區", 243: "新興區", 271: "鳥松區", 263: "橋頭區", 272: "大樹區", 274: "美濃區", 264: "梓官區", 273: "旗山區", 255: "大社區", 267: "湖內區", 282: "茄萣區", 262: "燕巢區", 260: "阿蓮區", 244: "前金區", 248: "旗津區", 246: "鹽埕區", 265: "彌陀區", 266: "永安區", 276: "內門區", 275: "六龜區", 277: "杉林區", 261: "田寮區", 278: "甲仙區", 279: "桃源區", 280: "那瑪夏區", 281: "茂林區"},
    19: {295: "屏東市", 308: "潮州鎮", 306: "內埔鄉", 307: "萬丹鄉", 316: "東港鎮", 319: "新園鄉", 326: "恆春鎮", 303: "長治鄉", 300: "里港鄉", 302: "鹽埔鄉", 320: "枋寮鄉", 301: "高樹鄉", 299: "九如鄉", 311: "萬巒鄉", 318: "佳冬鄉", 315: "林邊鄉", 305: "竹田鄉", 312: "崁頂鄉", 317: "琉球鄉", 304: "麟洛鄉", 314: "南州鄉", 313: "新埤鄉", 324: "車城鄉", 296: "三地門鄉", 310: "來義鄉", 327: "滿州鄉", 298: "瑪家鄉", 309: "泰武鄉", 321: "枋山鄉", 322: "春日鄉", 323: "獅子鄉", 325: "牡丹鄉", 297: "霧臺鄉"},
    21: {328: "宜蘭市", 333: "羅東鎮", 337: "冬山鄉", 336: "五結鄉", 338: "蘇澳鎮", 330: "礁溪鄉", 332: "員山鄉", 329: "頭城鎮", 331: "壯圍鄉", 334: "三星鄉", 339: "南澳鄉", 335: "大同鄉"},
    22: {341: "臺東市", 345: "卑南鄉", 351: "成功鎮", 353: "太麻里鄉", 347: "關山鎮", 350: "東河鄉", 349: "池上鄉", 346: "鹿野鄉", 352: "長濱鄉", 355: "大武鄉", 343: "蘭嶼鄉", 342: "綠島鄉", 348: "海端鄉", 354: "金峰鄉", 344: "延平鄉", 356: "達仁鄉"},
    23: {357: "花蓮市", 360: "吉安鄉", 367: "玉里鎮", 358: "新城鄉", 359: "秀林鄉", 361: "壽豐鄉", 363: "光復鄉", 365: "瑞穗鄉", 362: "鳳林鎮", 369: "富里鄉", 366: "萬榮鄉", 368: "卓溪鄉", 364: "豐濱鄉"},
    24: {283: "馬公市", 288: "湖西鄉", 287: "白沙鄉", 284: "西嶼鄉", 285: "望安鄉", 286: "七美鄉"},
    25: {292: "金城鎮", 291: "金寧鄉", 290: "金湖鎮", 289: "金沙鎮", 293: "烈嶼鄉", 294: "烏坵鄉"},
    26: {22: "南竿鄉", 23: "北竿鄉", 25: "東引鄉", 24: "莒光鄉", 256: "東沙", 257: "南沙"},
}

# Rental listing types used by 591's `kind` query parameter.
KINDS = {
    1: "整層住家",
    2: "獨立套房",
    3: "分租套房",
    4: "雅房",
    8: "車位",
    24: "其他",
}


def _resolve_region(region):
    """Resolve a region id or name (3 or "新北市") to its numeric id."""
    if isinstance(region, bool):
        raise ValueError("region must be an id or name, not a boolean")
    if isinstance(region, str) and not region.isdigit():
        for rid, name in REGIONS.items():
            if name == region:
                return rid
        raise ValueError(
            f"unknown region {region!r}; valid names: {', '.join(REGIONS.values())}"
        )
    rid = int(region)
    if rid not in REGIONS:
        raise ValueError(f"unknown region id {rid}; valid ids: {sorted(REGIONS)}")
    return rid


def _resolve_sections(region_id, sections):
    """Resolve section id(s) or name(s) within a region to a list of ids."""
    if sections is None:
        return []
    if isinstance(sections, (str, int)):
        sections = [sections]
    valid = SECTIONS.get(region_id, {})
    ids = []
    for s in sections:
        if isinstance(s, bool):
            raise ValueError("section must be an id or name, not a boolean")
        if isinstance(s, str) and not s.isdigit():
            sid = next((k for k, v in valid.items() if v == s), None)
            if sid is None:
                raise ValueError(
                    f"unknown section {s!r} in {REGIONS[region_id]}; "
                    f"valid names: {', '.join(valid.values())}"
                )
        else:
            sid = int(s)
            if sid not in valid:
                raise ValueError(
                    f"unknown section id {sid} in {REGIONS[region_id]}; "
                    f"valid ids: {sorted(valid)}"
                )
        ids.append(sid)
    return ids


def _resolve_kinds(kinds):
    """Resolve listing kind id(s) or name(s) to a list of 591 kind ids."""
    if kinds is None:
        return []
    if isinstance(kinds, (str, int)):
        kinds = [kinds]
    if not isinstance(kinds, list):
        raise ValueError("'kinds' must be a kind or a list of kinds")

    ids = []
    for kind in kinds:
        if isinstance(kind, bool):
            raise ValueError("listing kind must be an id or name, not a boolean")
        if isinstance(kind, str) and not kind.isdigit():
            kind_id = next((key for key, value in KINDS.items() if value == kind), None)
            if kind_id is None:
                raise ValueError(
                    f"unknown listing kind {kind!r}; "
                    f"valid names: {', '.join(KINDS.values())}"
                )
        else:
            try:
                kind_id = int(kind)
            except (TypeError, ValueError):
                raise ValueError(f"invalid listing kind {kind!r}") from None
            if kind_id not in KINDS:
                raise ValueError(
                    f"unknown listing kind id {kind_id}; valid ids: {sorted(KINDS)}"
                )
        if kind_id not in ids:
            ids.append(kind_id)
    return ids


def _validate_price_range(price_min, price_max):
    """Validate optional monthly rent bounds and return normalized values."""
    values = []
    for name, value in (("price_min", price_min), ("price_max", price_max)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"'{name}' must be a non-negative integer")
        values.append(value)
    price_min, price_max = values
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ValueError("'price_min' cannot be greater than 'price_max'")
    return price_min, price_max


def _text(node):
    """Return stripped text of a BeautifulSoup node, or '' if node is None."""
    return node.get_text(strip=True) if node else ""


def _parse_spec(spans):
    """Parse the spec line (kind/layout/area/floor) from its span fragments."""
    spec = {"kind": "", "layout": "", "area": "", "floor": ""}
    for frag in spans:
        if "坪" in frag:
            spec["area"] = frag
        elif re.match(r"^\d+\s*房", frag):  # e.g. "4房2廳2衛"
            spec["layout"] = frag
        elif "F" in frag or "樓" in frag:
            spec["floor"] = frag
        elif not spec["kind"]:
            spec["kind"] = frag  # e.g. 整層住家 / 獨立套房 / 雅房
    return spec


def _parse_item(item):
    """Parse one <div class="item"> listing element into a dict."""
    listing = {"id": item.get("data-id")}

    title_a = item.select_one("div.item-info-title a.link")
    if title_a is None:
        raise ValueError(f"listing {listing['id']} has no title link")
    listing["title"] = title_a.get("title") or _text(title_a)
    listing["url"] = title_a.get("href") or ""
    if not listing["title"] or not listing["url"]:
        raise ValueError(f"listing {listing['id']} has an incomplete title link")

    img = item.select_one("div.item-img img")
    # Lazy-loaded images keep an inline SVG placeholder in `src`.
    candidates = [img.get("data-src"), img.get("src")] if img else []
    listing["image"] = next(
        (c for c in candidates if c and not c.startswith("data:")), ""
    )

    price_node = item.select_one("div.item-info-price")
    listing["price"] = _text(price_node)
    strong = price_node.select_one("strong") if price_node else None
    price_digits = _text(strong).replace(",", "")
    listing["price_value"] = int(price_digits) if price_digits.isdigit() else None

    listing["tags"] = [_text(t) for t in item.select("div.item-info-tag span.tag")]
    preferred = item.select_one("div.item-info-title span.tag")
    if preferred:
        listing["tags"].insert(0, _text(preferred))

    for txt in item.select("div.item-info-txt"):
        icon = txt.select_one("i")
        icon_class = " ".join(icon.get("class", [])) if icon else ""
        spans = [_text(s) for s in txt.find_all("span", recursive=False)]

        if "role-name" in txt.get("class", []):
            listing["poster"] = spans[0] if spans else ""
            for frag in spans[1:]:
                if "更新" in frag:
                    listing["updated"] = frag
                elif "瀏覽" in frag:
                    listing["views"] = frag
        elif "house-home" in icon_class:
            listing.update(_parse_spec(spans))
        elif "house-place" in icon_class:
            # May include a community name followed by "區-路" address fragment.
            listing["community"] = spans[0] if len(spans) > 1 else ""
            listing["location"] = spans[-1] if spans else ""
        elif "house-metro" in icon_class or "house-bus-line" in icon_class:
            station = _text(txt)
            listing["nearby_transit"] = {
                "type": "metro" if "house-metro" in icon_class else "bus",
                "text": station,
            }

    return listing


def crawl_rent_list(
    region=3,
    sections=None,
    kinds=None,
    price_min=None,
    price_max=None,
    sort="posttime_desc",
    page=None,
    timeout=30,
):
    """Fetch a 591 rent list page and return its listings as a JSON string.

    Args:
        region: Region (縣市) id or name, e.g. 3 or "新北市".
        sections: Section (鄉鎮市區) id(s) or name(s) within the region,
            e.g. 39, "土城區", or ["土城區", "中和區"]. None means all.
        kinds: Listing kind id(s) or name(s), e.g. "整層住家" or
            ["整層住家", "獨立套房"]. None means all kinds.
        price_min: Optional minimum monthly rent in NTD.
        price_max: Optional maximum monthly rent in NTD.
        sort: Sort order, e.g. "posttime_desc" (newest first) or "posttime_asc".
        page: Optional page number (30 listings per page).
        timeout: HTTP request timeout in seconds.

    Returns:
        A JSON string: {"url", "region", "sections", "count", "listings"}.
    """
    region_id = _resolve_region(region)
    section_ids = _resolve_sections(region_id, sections)
    kind_ids = _resolve_kinds(kinds)
    price_min, price_max = _validate_price_range(price_min, price_max)

    params = {"region": region_id, "sort": sort}
    if section_ids:
        params["section"] = ",".join(map(str, section_ids))
    if kind_ids:
        params["kind"] = ",".join(map(str, kind_ids))
    if price_min is not None or price_max is not None:
        low = 0 if price_min is None else price_min
        high = "" if price_max is None else price_max
        params["price"] = f"{low}_{high}"
    if page is not None:
        params["page"] = int(page)
    url = f"{BASE_URL}?{urlencode(params)}"

    resp = _http_get(url, timeout=timeout)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    # The saved fixture uses the legacy id while the current Nuxt page uses
    # .list-wrapper. Requiring one of these prevents challenge/login HTML from
    # being mistaken for a valid empty result.
    container = soup.select_one("#list-container, .list-wrapper")
    if container is None:
        raise CrawlerParseError(
            "591 response did not contain the expected listing container"
        )
    cards = [
        item for item in container.select("div.item[data-id]") if item.get("data-id")
    ]
    listings = []
    parse_errors = []
    for item in cards:
        try:
            listings.append(_parse_item(item))
        except (AttributeError, TypeError, ValueError) as exc:
            listing_id = str(item.get("data-id") or "unknown")
            parse_errors.append({"id": listing_id, "error": str(exc)})
            LOGGER.warning("Skipping malformed 591 listing %s: %s", listing_id, exc)
    if cards and not listings:
        raise CrawlerParseError(
            f"all {len(cards)} listing cards failed to parse; site markup may have changed"
        )
    if not cards:
        page_text = soup.get_text(" ", strip=True)
        empty_markers = ("查無符合", "沒有符合", "暫無符合", "沒有相關物件")
        if not any(marker in page_text for marker in empty_markers):
            raise CrawlerParseError(
                "listing container had no cards and no recognized empty-result message"
            )

    return json.dumps(
        {
            "url": url,
            "region": REGIONS[region_id],
            "sections": [SECTIONS[region_id][sid] for sid in section_ids],
            "kinds": [KINDS[kind_id] for kind_id in kind_ids],
            "price": {"min": price_min, "max": price_max},
            "count": len(listings),
            "parse_error_count": len(parse_errors),
            "parse_errors": parse_errors,
            "listings": listings,
        },
        ensure_ascii=False,
        indent=2,
    )


def _label_value_pairs(container, item_sel, label_sel, value_sel):
    """Extract {label: value} pairs from repeated item blocks."""
    pairs = {}
    for item in container.select(item_sel):
        label = _text(item.select_one(label_sel))
        value = _text(item.select_one(value_sel))
        if label:
            pairs[label] = value
    return pairs


def _parse_detail(soup, url):
    """Parse one rent detail page into a dict."""
    detail = {"url": url}

    m = re.search(r"rent\.591\.com\.tw/(\d+)", url)
    detail["id"] = m.group(1) if m else None

    board = soup.select_one("section.info-board") or soup
    detail["title"] = _text(board.select_one("h1"))
    detail["preferred"] = board.select_one("span.preferred-tag") is not None
    detail["labels"] = [
        _text(s) for s in board.select("div.house-label span.label-item")
    ]

    # Pattern line: layout / area / floor / building type (e.g. 公寓、透天).
    spans = [
        _text(s)
        for s in board.select("div.pattern > span")
        if _text(s)
    ]
    spec = _parse_spec(spans)
    detail["layout"] = spec["layout"]
    detail["area"] = spec["area"]
    detail["floor"] = spec["floor"]
    detail["building_type"] = spec["kind"]

    price_node = board.select_one("div.house-price span.c-price")
    detail["price"] = re.sub(r"\s+", "", _text(price_node))
    strong = price_node.select_one("strong") if price_node else None
    num = _text(strong).replace(",", "")
    detail["price_value"] = int(num) if num.isdigit() else None

    detail["address"] = _text(soup.select_one("div.address span.load-map"))

    # 房屋詳情: grouped label/value sections such as 基礎資料、房屋價格.
    details = {}
    for section in soup.select("section.detail-section"):
        name = _text(section.select_one("span.section-name"))
        pairs = _label_value_pairs(
            section, "div.item", "span.label", "span.value"
        )
        if name and pairs:
            details[name] = pairs
    detail["details"] = details

    # 租住說明: 最短租期 / 身份要求 / 養寵物 ...
    service = soup.select_one("section.block.service")
    detail["rental_notes"] = (
        _label_value_pairs(service, "div.desc-item", "span.desc-label", "span.desc-value")
        if service
        else {}
    )

    # 提供設備/家具: <dl class="del"> marks items NOT provided.
    provided, not_provided = [], []
    for dl in soup.select("div.facility dl"):
        name = _text(dl.select_one("dd"))
        if not name:
            continue
        (not_provided if "del" in (dl.get("class") or []) else provided).append(name)
    detail["facilities"] = {"provided": provided, "not_provided": not_provided}

    article = soup.select_one("div.house-condition-content div.article")
    if article:
        text = article.get_text(separator="\n", strip=True)
        detail["description"] = text.replace("\xa0", " ")
    else:
        detail["description"] = ""

    detail["poster"] = _text(soup.select_one("section.contact p.base-info-pc span.name"))
    detail["poster_info"] = _text(soup.select_one("section.contact div.company-info p"))

    images = []
    for img in soup.select("section.album img"):
        for attr in ("data-src", "src"):
            src = img.get(attr) or ""
            if src.startswith("http"):
                if src not in images:
                    images.append(src)
                break
    detail["images"] = images

    return detail


def crawl_rent_details(urls, timeout=30, delay=0.5):
    """Fetch one or more 591 rent detail pages and return them as a JSON string.

    Args:
        urls: A single detail page URL or a list of URLs,
            e.g. "https://rent.591.com.tw/21803874".
        timeout: HTTP request timeout in seconds.
        delay: Pause in seconds between requests when crawling multiple URLs.

    Returns:
        A JSON string: {"count": N, "listings": [...]}. A URL that fails to
        fetch yields an entry with only "url" and "error" keys.
    """
    if isinstance(urls, str):
        urls = [urls]

    listings = []
    requested = 0
    for url in urls:
        try:
            url = _validate_detail_url(url)
        except ValueError as exc:
            listings.append({"url": str(url), "error": str(exc)})
            continue
        if requested:
            time.sleep(delay)
        requested += 1
        try:
            resp = _http_get(url, timeout=timeout)
            resp.raise_for_status()
            listings.append(_parse_detail(BeautifulSoup(resp.text, "html.parser"), url))
        except (requests.RequestException, AttributeError, TypeError, ValueError) as exc:
            LOGGER.warning("Could not crawl 591 detail %s: %s", url, exc)
            listings.append({"url": url, "error": "request failed"})

    return json.dumps(
        {"count": len(listings), "listings": listings},
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    print(crawl_rent_list(region="新北市"))
