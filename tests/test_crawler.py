"""Tests for the 591 rent list crawler.

Unit tests run offline against a saved HTML fixture and a mocked HTTP layer.
Integration tests hit the live site and are skipped by default; run them with:

    pytest -m integration
"""

import json
import ssl
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from bs4 import BeautifulSoup

from rent591_notifier import crawler
from rent591_notifier.crawler import (
    KINDS,
    REGIONS,
    SECTIONS,
    CrawlerParseError,
    _CompatibilityTLSAdapter,
    _http_get,
    _parse_detail,
    _parse_item,
    _parse_spec,
    _resolve_kinds,
    _resolve_region,
    _resolve_sections,
    _validate_price_range,
    crawl_rent_details,
    crawl_rent_list,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_HTML = (FIXTURES / "list_page.html").read_text(encoding="utf-8")
DETAIL_FIXTURE_HTML = (FIXTURES / "detail_page.html").read_text(encoding="utf-8")


def test_compatibility_tls_adapter_keeps_verification_enabled():
    adapter = _CompatibilityTLSAdapter()
    context = adapter.poolmanager.connection_pool_kw["ssl_context"]

    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        assert not context.verify_flags & strict


def test_compatibility_tls_adapter_supports_python_without_strict_flag(monkeypatch):
    monkeypatch.delattr(crawler.ssl, "VERIFY_X509_STRICT")

    adapter = _CompatibilityTLSAdapter()

    assert adapter.poolmanager.connection_pool_kw["ssl_context"].check_hostname


def parse_fixture_items():
    soup = BeautifulSoup(FIXTURE_HTML, "html.parser")
    return [
        _parse_item(item)
        for item in soup.select("div.item[data-id]")
        if item.get("data-id")
    ]


# ---------------------------------------------------------------------------
# _resolve_region
# ---------------------------------------------------------------------------


class TestResolveRegion:
    def test_id_passthrough(self):
        assert _resolve_region(3) == 3

    def test_numeric_string(self):
        assert _resolve_region("3") == 3

    def test_name(self):
        assert _resolve_region("新北市") == 3

    @pytest.mark.parametrize("name", REGIONS.values())
    def test_every_region_name_resolves(self, name):
        assert REGIONS[_resolve_region(name)] == name

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="unknown region '宇宙市'"):
            _resolve_region("宇宙市")

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="unknown region id 999"):
            _resolve_region(999)


# ---------------------------------------------------------------------------
# _resolve_sections
# ---------------------------------------------------------------------------


class TestResolveSections:
    def test_none_means_all(self):
        assert _resolve_sections(3, None) == []

    def test_single_id(self):
        assert _resolve_sections(3, 39) == [39]

    def test_single_name(self):
        assert _resolve_sections(3, "土城區") == [39]

    def test_list_of_names(self):
        assert _resolve_sections(3, ["土城區", "中和區"]) == [39, 38]

    def test_mixed_id_and_name(self):
        assert _resolve_sections(3, [39, "中和區"]) == [39, 38]

    def test_ambiguous_names_resolve_within_region(self):
        # The label `東區` exists in several cities; each region resolves its own.
        assert _resolve_sections(4, "東區") == [371]  # Expected ID for 新竹市.
        assert _resolve_sections(8, "東區") == [99]  # Expected ID for 台中市.

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="unknown section '火星區'"):
            _resolve_sections(3, "火星區")

    def test_section_from_wrong_region_raises(self):
        # `北屯區` belongs to `台中市`, not `新北市`.
        with pytest.raises(ValueError, match="unknown section '北屯區'"):
            _resolve_sections(3, "北屯區")

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="unknown section id 999"):
            _resolve_sections(3, 999)

    @pytest.mark.parametrize("region_id", SECTIONS.keys())
    def test_every_section_name_resolves(self, region_id):
        for sid, name in SECTIONS[region_id].items():
            assert _resolve_sections(region_id, name) == [sid]


# ---------------------------------------------------------------------------
# listing kind and price filters
# ---------------------------------------------------------------------------


class TestResolveKinds:
    def test_none_means_all(self):
        assert _resolve_kinds(None) == []

    def test_names_and_ids(self):
        assert _resolve_kinds(["整層住家", 2, "8"]) == [1, 2, 8]

    def test_duplicates_are_removed(self):
        assert _resolve_kinds(["獨立套房", 2]) == [2]

    @pytest.mark.parametrize("kind_id, name", KINDS.items())
    def test_every_kind_resolves(self, kind_id, name):
        assert _resolve_kinds(name) == [kind_id]

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown listing kind"):
            _resolve_kinds("城堡")


class TestPriceRange:
    def test_bounded_range(self):
        assert _validate_price_range(10000, 30000) == (10000, 30000)

    def test_open_range(self):
        assert _validate_price_range(None, 20000) == (None, 20000)

    @pytest.mark.parametrize("bounds", [(-1, 10000), (0, 1.5), (True, 10000)])
    def test_invalid_bound_raises(self, bounds):
        with pytest.raises(ValueError, match="non-negative integer"):
            _validate_price_range(*bounds)

    def test_reversed_range_raises(self):
        with pytest.raises(ValueError, match="cannot be greater"):
            _validate_price_range(30000, 10000)


# ---------------------------------------------------------------------------
# _parse_spec
# ---------------------------------------------------------------------------


class TestParseSpec:
    def test_full_spec(self):
        spec = _parse_spec(["整層住家", "4房2廳", "43坪", "3F/3F"])
        assert spec == {
            "kind": "整層住家",
            "layout": "4房2廳",
            "area": "43坪",
            "floor": "3F/3F",
        }

    def test_suite_has_no_layout(self):
        # `獨立套房` is the listing kind, not a layout.
        spec = _parse_spec(["獨立套房", "4坪", "2F/4F"])
        assert spec == {
            "kind": "獨立套房",
            "layout": "",
            "area": "4坪",
            "floor": "2F/4F",
        }

    def test_parking_has_kind_and_area_only(self):
        spec = _parse_spec(["車位", "8坪"])
        assert spec["kind"] == "車位"
        assert spec["area"] == "8坪"
        assert spec["layout"] == ""
        assert spec["floor"] == ""

    def test_empty(self):
        assert _parse_spec([]) == {
            "kind": "",
            "layout": "",
            "area": "",
            "floor": "",
        }


# ---------------------------------------------------------------------------
# _parse_item (against the saved fixture)
# ---------------------------------------------------------------------------


class TestParseItem:
    def test_skips_items_without_data_id(self):
        # The fixture contains an ad block with class="item" but no data-id.
        items = parse_fixture_items()
        assert len(items) == 3
        assert all(i["id"] for i in items)

    def test_full_house_item(self):
        item = parse_fixture_items()[0]
        assert item["id"] == "21803880"
        assert item["title"] == "💗台北爵士｜全新極美日系奶油風3房2廳2衛｜含平面車位"
        assert item["url"] == "https://rent.591.com.tw/21803880"
        assert item["image"].startswith("https://img1.591.com.tw/")
        assert item["price"] == "36,800元/月"
        assert item["price_value"] == 36800
        assert item["tags"] == ["新上架", "拎包入住", "隨時可遷入", "有車位"]
        assert item["kind"] == "整層住家"
        assert item["layout"] == "3房2廳"
        assert item["area"] == "38.6坪"
        assert item["floor"] == "9F/14F"
        assert item["community"] == "台北爵士"
        assert item["location"] == "汐止區-福德一路"
        assert item["nearby_transit"] == {
            "type": "bus",
            "text": "距伯爵山莊站386公尺",
        }
        assert item["poster"] == "仲介吳先生"
        assert item["updated"] == "10分鐘內更新"
        assert item["views"] == "昨日0人瀏覽"

    def test_suite_item_with_lazy_image_and_preferred_tag(self):
        item = parse_fixture_items()[1]
        assert item["id"] == "21803973"
        # Lazy-loaded image: real URL comes from data-src, not the SVG src.
        assert item["image"] == (
            "https://img2.591.com.tw/video/cover/2026-08-10/3201363.png"
            "!1000x.water2.png"
        )
        # Preferred tag is prepended to the tag list.
        assert item["tags"][0] == "優選好屋"
        assert "近捷運" in item["tags"]
        assert item["kind"] == "獨立套房"
        assert item["layout"] == ""
        assert item["price"] == "9,000元/月(租金含水費/網路/第四臺)"
        assert item["price_value"] == 9000
        assert item["nearby_transit"]["type"] == "metro"

    def test_parking_item_without_photo(self):
        item = parse_fixture_items()[2]
        assert item["id"] == "21803801"
        # Only an inline SVG placeholder exists -> no image URL.
        assert item["image"] == ""
        assert item["kind"] == "車位"
        assert item["layout"] == ""
        assert item["floor"] == ""
        assert item["price_value"] == 3000

    def test_missing_title_link_is_a_clear_parse_error(self):
        soup = BeautifulSoup('<div class="item" data-id="broken"></div>', "html.parser")
        with pytest.raises(ValueError, match="no title link"):
            _parse_item(soup.div)


# ---------------------------------------------------------------------------
# crawl_rent_list (HTTP mocked with the fixture)
# ---------------------------------------------------------------------------


def _mock_response(html):
    resp = mock.Mock()
    resp.text = html
    resp.raise_for_status = mock.Mock()
    return resp


class TestCrawlRentList:
    def _crawl(self, **kwargs):
        with mock.patch("rent591_notifier.crawler._http_get") as mget:
            mget.return_value = _mock_response(FIXTURE_HTML)
            result = json.loads(crawl_rent_list(**kwargs))
        return result, mget

    def test_default_url(self):
        result, mget = self._crawl()
        url = mget.call_args.args[0]
        assert parse_qs(urlparse(url).query)["region"] == ["3"]
        assert parse_qs(urlparse(url).query)["sort"] == ["posttime_desc"]
        assert "section" not in urlparse(url).query
        assert "page" not in urlparse(url).query
        assert result["region"] == "新北市"
        assert result["sections"] == []
        assert result["count"] == 3
        assert len(result["listings"]) == 3

    def test_sections_and_page_in_url(self):
        result, mget = self._crawl(
            region="新北市", sections=["土城區", "中和區"], page=2
        )
        qs = parse_qs(urlparse(mget.call_args.args[0]).query)
        assert qs["region"] == ["3"]
        assert qs["section"] == ["39,38"]
        assert qs["page"] == ["2"]
        assert result["sections"] == ["土城區", "中和區"]

    def test_kind_and_price_filters_in_url(self):
        result, mget = self._crawl(
            kinds=["整層住家", "獨立套房"],
            price_min=10000,
            price_max=30000,
        )
        qs = parse_qs(urlparse(mget.call_args.args[0]).query)
        assert qs["kind"] == ["1,2"]
        assert qs["price"] == ["10000_30000"]
        assert result["kinds"] == ["整層住家", "獨立套房"]
        assert result["price"] == {"min": 10000, "max": 30000}

    def test_open_ended_price_range_in_url(self):
        _, mget = self._crawl(price_max=20000)
        qs = parse_qs(urlparse(mget.call_args.args[0]).query)
        assert qs["price"] == ["0_20000"]

    def test_region_by_name_and_id_give_same_url(self):
        _, by_name = self._crawl(region="台中市", sections="北屯區")
        _, by_id = self._crawl(region=8, sections=103)
        assert by_name.call_args.args[0] == by_id.call_args.args[0]

    def test_json_shape_of_listing(self):
        result, _ = self._crawl()
        listing = result["listings"][0]
        for key in (
            "id",
            "title",
            "url",
            "image",
            "price",
            "price_value",
            "tags",
            "kind",
            "layout",
            "area",
            "floor",
            "community",
            "location",
            "nearby_transit",
            "poster",
            "updated",
            "views",
        ):
            assert key in listing, key

    def test_http_error_propagates(self):
        with mock.patch("rent591_notifier.crawler._http_get") as mget:
            mget.return_value.raise_for_status.side_effect = Exception("boom")
            with pytest.raises(Exception, match="boom"):
                crawl_rent_list()

    def test_invalid_region_raises_before_http(self):
        with mock.patch("rent591_notifier.crawler._http_get") as mget, pytest.raises(
            ValueError
        ):
            crawl_rent_list(region="宇宙市")
        mget.assert_not_called()

    def test_invalid_section_raises_before_http(self):
        with mock.patch("rent591_notifier.crawler._http_get") as mget, pytest.raises(
            ValueError
        ):
            crawl_rent_list(region=3, sections="北屯區")
        mget.assert_not_called()

    def test_unrecognizable_success_page_is_rejected(self):
        with (
            mock.patch(
                "rent591_notifier.crawler._http_get",
                return_value=_mock_response("<html>challenge</html>"),
            ),
            pytest.raises(CrawlerParseError, match="listing container"),
        ):
            crawl_rent_list()

    def test_one_malformed_card_does_not_discard_valid_cards(self):
        html = FIXTURE_HTML.replace(
            '<div class="item" data-id="21803880">',
            '<div class="item" data-id="broken"></div><div class="item" data-id="21803880">',
            1,
        )
        with mock.patch(
            "rent591_notifier.crawler._http_get", return_value=_mock_response(html)
        ):
            result = json.loads(crawl_rent_list())
        assert result["count"] == 3
        assert result["parse_error_count"] == 1
        assert result["parse_errors"][0]["id"] == "broken"

    def test_http_redirect_cannot_escape_591_host(self):
        redirect = mock.Mock(
            is_redirect=True,
            is_permanent_redirect=False,
            headers={"location": "http://127.0.0.1/internal"},
        )
        session = mock.Mock()
        session.get.return_value = redirect
        with (
            mock.patch("rent591_notifier.crawler._http_session", return_value=session),
            pytest.raises(ValueError, match="must stay on"),
        ):
            _http_get("https://rent.591.com.tw/1", timeout=1)
        session.get.assert_called_once()


# ---------------------------------------------------------------------------
# _parse_detail (against the saved fixture)
# ---------------------------------------------------------------------------


class TestParseDetail:
    def _parse(self, url="https://rent.591.com.tw/21803874"):
        soup = BeautifulSoup(DETAIL_FIXTURE_HTML, "html.parser")
        return _parse_detail(soup, url)

    def test_basic_fields(self):
        d = self._parse()
        assert d["url"] == "https://rent.591.com.tw/21803874"
        assert d["id"] == "21803874"
        assert d["title"] == "好宅租管【社宅可租補】捷運頂埔站步行6分鐘｜透天3層樓可寵"
        assert d["preferred"] is True
        assert d["labels"] == ["近捷運", "新上架", "免服務費", "可養寵物"]

    def test_pattern_fields(self):
        d = self._parse()
        assert d["layout"] == "4房2廳2衛"
        assert d["area"] == "43坪"
        assert d["floor"] == "3F/3F"
        assert d["building_type"] == "公寓"

    def test_price_excludes_extra_fee_noise(self):
        d = self._parse()
        assert d["price"] == "16,000元/月"
        assert d["price_value"] == 16000

    def test_address(self):
        assert self._parse()["address"] == "土城區中央路四段125巷"

    def test_detail_sections(self):
        d = self._parse()
        assert d["details"]["基礎資料"] == {"電梯": "無", "陽台": "2陽台"}
        assert d["details"]["房屋價格"]["押金"] == "二個月"
        assert d["details"]["房屋價格"]["管理費"] == "無"

    def test_rental_notes(self):
        d = self._parse()
        assert d["rental_notes"] == {
            "最短租期": "一年",
            "養寵物": "可養寵物",
            "開伙": "可開伙",
        }

    def test_facilities_split_by_del_class(self):
        d = self._parse()
        assert d["facilities"]["provided"] == ["冰箱", "洗衣機"]
        assert d["facilities"]["not_provided"] == ["網路", "天然瓦斯"]

    def test_description_keeps_line_breaks(self):
        d = self._parse()
        assert "第一行說明\n第二行說明" in d["description"]
        assert "注意事項" in d["description"]

    def test_poster(self):
        d = self._parse()
        assert d["poster"] == "仲介: 林小姐"
        assert d["poster_info"] == "經紀業: 禾豐好宅管理顧問股份有限公司"

    def test_images_prefer_data_src_and_dedup(self):
        d = self._parse()
        assert d["images"] == [
            "https://img2.591.com.tw/house/2026/08/10/178634320525224801.jpg!1000x.water2.jpg",
            "https://img1.591.com.tw/house/2026/08/10/178634320492218005.jpg!1000x.water2.jpg",
        ]

    def test_id_from_unparseable_url_is_none(self):
        d = self._parse(url="https://example.com/foo")
        assert d["id"] is None


# ---------------------------------------------------------------------------
# crawl_rent_details (HTTP mocked with the fixture)
# ---------------------------------------------------------------------------


class TestCrawlRentDetails:
    def _crawl(self, arg, **kwargs):
        with mock.patch("rent591_notifier.crawler._http_get") as mget:
            mget.return_value = _mock_response(DETAIL_FIXTURE_HTML)
            result = json.loads(crawl_rent_details(arg, **kwargs))
        return result, mget

    def test_single_url_string(self):
        result, mget = self._crawl("https://rent.591.com.tw/21803874", delay=0)
        assert result["count"] == 1
        assert result["listings"][0]["id"] == "21803874"
        assert result["listings"][0]["price_value"] == 16000
        mget.assert_called_once()

    def test_list_of_urls(self):
        urls = ["https://rent.591.com.tw/1", "https://rent.591.com.tw/2"]
        with mock.patch("rent591_notifier.crawler.time.sleep") as msleep:
            result, mget = self._crawl(urls)
        assert result["count"] == 2
        assert mget.call_count == 2
        # Delay applies between requests, not before the first one.
        msleep.assert_called_once_with(0.5)

    def test_failed_url_yields_error_entry(self):
        with mock.patch("rent591_notifier.crawler._http_get") as mget:
            ok = _mock_response(DETAIL_FIXTURE_HTML)
            bad = mock.Mock()
            bad.raise_for_status.side_effect = requests.HTTPError("404")
            mget.side_effect = [bad, ok]
            result = json.loads(
                crawl_rent_details(
                    ["https://rent.591.com.tw/1", "https://rent.591.com.tw/2"],
                    delay=0,
                )
            )
        assert "error" in result["listings"][0]
        assert result["listings"][1]["id"] == "2"

    @pytest.mark.parametrize(
        "url",
        [
            "http://rent.591.com.tw/1",
            "https://example.com/1",
            "https://rent.591.com.tw.evil.example/1",
            "https://rent.591.com.tw/not-a-number",
        ],
    )
    def test_rejects_untrusted_detail_urls_before_http(self, url):
        with mock.patch("rent591_notifier.crawler._http_get") as mget:
            result = json.loads(crawl_rent_details(url, delay=0))
        assert "error" in result["listings"][0]
        mget.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tests against the live site (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLiveSite:
    def test_default_page(self):
        data = json.loads(crawl_rent_list())
        assert data["count"] > 0
        for listing in data["listings"]:
            assert listing["id"] and listing["title"] and listing["url"]

    def test_section_filter(self):
        data = json.loads(crawl_rent_list(region="新北市", sections="土城區"))
        assert data["count"] > 0
        for listing in data["listings"]:
            assert listing["location"].startswith("土城區")

    def test_detail_page(self):
        page = json.loads(crawl_rent_list())
        source = page["listings"][0]
        data = json.loads(crawl_rent_details(source["url"]))
        listing = data["listings"][0]
        assert listing["id"] == source["id"]
        assert listing["title"]
        assert listing["price_value"] > 0
        assert listing["address"]
        assert listing["images"]
