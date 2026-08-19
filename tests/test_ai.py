"""Tests for OpenCode Go and Zen listing evaluation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rent591_notifier import ai


def detail_payload():
    return json.dumps(
        {
            "listings": [
                {
                    "title": "採光兩房",
                    "address": "新北市土城區中央路100號",
                    "layout": "2房1廳1衛",
                    "area": "20坪",
                    "floor": "5F/12F",
                    "building_type": "電梯大樓",
                    "labels": ["近捷運"],
                    "details": {"基礎資料": {"型態": "電梯大樓"}},
                    "rental_notes": {"養寵物": "不可"},
                    "facilities": {
                        "provided": ["冷氣", "洗衣機"],
                        "not_provided": ["瓦斯爐"],
                    },
                    "description": "客廳採光佳，距捷運站步行五分鐘。",
                    "poster": "王小姐",
                    "poster_info": "屋主",
                    "images": ["https://img1.591.com.tw/one.jpg"],
                }
            ]
        },
        ensure_ascii=False,
    )


def listing():
    return {
        "id": "123",
        "title": "採光兩房",
        "url": "https://rent.591.com.tw/123",
        "price": "20,000元/月",
        "location": "土城區-中央路",
        "kind": "整層住家",
        "tags": ["可開伙"],
        "nearby_transit": {"type": "metro", "text": "海山站"},
    }


def chat_response(content):
    return SimpleNamespace(
        status_code=200,
        text="",
        json=lambda: {"choices": [{"message": {"content": content}}]},
    )


def test_evaluate_listing_sends_full_detail_and_images_go(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 8, "reason": "採光佳"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(
        ai, "_download_image", lambda _: "data:image/jpeg;base64,aW1hZ2U="
    )

    verdict, images = ai.evaluate_listing(
        {"provider": "go", "model": "kimi-k3", "criteria": "重視採光", "max_images": 6},
        "新北市",
        listing(),
        api_key="test-key",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict == {"good": True, "score": 8, "reason": "採光佳"}
    assert images == ["https://img1.591.com.tw/one.jpg"]
    request = post.call_args.kwargs["json"]
    assert request["model"] == "kimi-k3"
    assert request["response_format"] == {"type": "json_object"}
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}
    # Verify Go base URL was used
    assert post.call_args.args[0] == "https://opencode.ai/zen/go/v1/chat/completions"
    user_content = request["messages"][1]["content"]
    prompt = user_content[0]["text"]
    assert "完整地址：新北市土城區中央路100號" in prompt
    assert "租住說明：養寵物：不可" in prompt
    assert "提供設備：冷氣、洗衣機" in prompt
    assert "屋況描述：客廳採光佳" in prompt
    assert user_content[1]["type"] == "image_url"


def test_evaluate_listing_uses_zen_provider(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 7, "reason": "CP值高"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(
        ai, "_download_image", lambda _: "data:image/jpeg;base64,aW1hZ2U="
    )

    verdict, _ = ai.evaluate_listing(
        {
            "provider": "zen",
            "model": "mimo-v2.5-free",
            "criteria": "預算優先",
            "max_images": 4,
        },
        "台北市",
        listing(),
        api_key="zen-key",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict == {"good": True, "score": 7, "reason": "CP值高"}
    # Verify Zen base URL was used
    assert post.call_args.args[0] == "https://opencode.ai/zen/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "mimo-v2.5-free"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer zen-key"}


def test_evaluate_listing_zen_without_api_key(monkeypatch):
    """Zen provider should work without API key for free models."""
    post = MagicMock(
        return_value=chat_response('{"good": false, "score": 2, "reason": "租金偏高"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai, "_download_image", lambda _: None)

    verdict, _ = ai.evaluate_listing(
        {"provider": "zen", "model": "mimo-v2.5-free", "criteria": "預算優先"},
        "新北市",
        listing(),
        api_key=None,  # No API key for free models
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict["good"] is False
    # No Authorization header when api_key is None
    assert "Authorization" not in post.call_args.kwargs.get("headers", {})


def test_evaluate_listing_go_requires_api_key():
    """Go provider should fail when no API key is provided."""
    with pytest.raises(ai.AIEvaluationError, match="OPENCODE_GO_API_KEY is not set"):
        ai.evaluate_listing(
            {"provider": "go", "model": "kimi-k3"},
            "新北市",
            listing(),
            api_key=None,
            detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
        )


def test_evaluate_listing_retries_without_json_mode_after_bad_request(monkeypatch):
    bad_request = SimpleNamespace(status_code=400, text="unsupported", json=MagicMock())
    post = MagicMock(
        side_effect=[
            bad_request,
            chat_response('{"good": false, "score": 3, "reason": "租金偏高"}'),
        ]
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(
        ai, "_download_image", lambda _: "data:image/jpeg;base64,aW1hZ2U="
    )

    verdict, _ = ai.evaluate_listing(
        {},
        "新北市",
        listing(),
        api_key="test-key",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict["good"] is False
    assert post.call_count == 2
    assert "response_format" in post.call_args_list[0].kwargs["json"]
    assert "response_format" not in post.call_args_list[1].kwargs["json"]
    assert len(post.call_args_list[1].kwargs["json"]["messages"][1]["content"]) == 2


def test_download_image_rejects_non_591_hosts(monkeypatch):
    request = MagicMock()
    monkeypatch.setattr(ai.requests, "get", request)

    assert ai._download_image("https://example.com/image.jpg") is None
    request.assert_not_called()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '分析完成： {"good": "false", "score": 12, "reason": "偏貴"}',
            {"good": False, "score": 10, "reason": "偏貴"},
        ),
        (
            '{"good": true, "reason": "條件合理"}',
            {"good": True, "score": None, "reason": "條件合理"},
        ),
    ],
)
def test_parse_verdict_normalizes_model_output(text, expected):
    assert ai._parse_verdict(text) == expected
