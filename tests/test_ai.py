"""Tests for OpenAI-compatible listing evaluation."""

import json
import ssl
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


def test_evaluate_listing_sends_full_detail_and_images(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 8, "reason": "採光佳"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(
        ai, "_download_image", lambda _: "data:image/jpeg;base64,aW1hZ2U="
    )

    verdict, images = ai.evaluate_listing(
        {
            "api_endpoint": "https://provider.example/v1",
            "api_key": "test-key",
            "criteria": "重視採光",
            "max_images": 6,
        },
        "新北市",
        listing(),
        model="test-model",
        crawl_filters={
            "sections": ["土城區"],
            "kinds": ["整層住家"],
            "price_min": 10000,
            "price_max": 30000,
        },
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict == {"good": True, "score": 8, "reason": "採光佳"}
    assert images == ["https://img1.591.com.tw/one.jpg"]
    request = post.call_args.kwargs["json"]
    assert request["model"] == "test-model"
    assert request["response_format"] == {"type": "json_object"}
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}
    assert post.call_args.kwargs["timeout"] == 60
    assert post.call_args.args[0] == "https://provider.example/v1/chat/completions"
    user_content = request["messages"][1]["content"]
    prompt = user_content[0]["text"]
    assert "行政區：土城區" in prompt
    assert "物件類型：整層住家" in prompt
    assert "租金範圍：NT$10,000～30,000" in prompt
    assert "完整地址：新北市土城區中央路100號" in prompt
    assert "租住說明：養寵物：不可" in prompt
    assert "提供設備：冷氣、洗衣機" in prompt
    assert "屋況描述：客廳採光佳" in prompt
    assert user_content[1]["type"] == "image_url"


def test_evaluate_listing_uses_configured_endpoint(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 7, "reason": "CP值高"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(
        ai, "_download_image", lambda _: "data:image/jpeg;base64,aW1hZ2U="
    )

    verdict, _ = ai.evaluate_listing(
        {
            "api_endpoint": "https://provider.example/custom/v1",
            "api_key": "zen-key",
            "criteria": "預算優先",
            "max_images": 4,
        },
        "台北市",
        listing(),
        model="custom-model",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict == {"good": True, "score": 7, "reason": "CP值高"}
    assert (
        post.call_args.args[0] == "https://provider.example/custom/v1/chat/completions"
    )
    assert post.call_args.kwargs["json"]["model"] == "custom-model"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer zen-key"}


def test_evaluate_listing_accepts_full_chat_completions_endpoint(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 7, "reason": "推薦"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai, "_download_image", lambda _: None)

    ai.evaluate_listing(
        {
            "api_endpoint": "https://provider.example/v1/chat/completions",
            "api_key": "test-key",
        },
        "台北市",
        listing(),
        model="openai/gpt-4o",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert post.call_args.args[0] == "https://provider.example/v1/chat/completions"


def test_evaluate_listing_requires_all_connection_settings():
    with pytest.raises(ai.AIEvaluationError, match="API key"):
        ai.evaluate_listing(
            {"api_endpoint": "https://provider.example/v1"},
            "新北市",
            listing(),
            model="test-model",
            detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
        )


def test_evaluate_listing_uses_configured_api_key(monkeypatch):
    post = MagicMock(
        return_value=chat_response('{"good": true, "score": 8, "reason": "推薦"}')
    )
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai, "_download_image", lambda _: None)
    ai.evaluate_listing(
        {
            "api_endpoint": "https://provider.example/v1",
            "api_key": "saved-key",
        },
        "新北市",
        listing(),
        model="test-model",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer saved-key"}


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
        {
            "api_endpoint": "https://provider.example/v1",
            "api_key": "test-key",
        },
        "新北市",
        listing(),
        model="test-model",
        detail_fetcher=lambda *_args, **_kwargs: detail_payload(),
    )

    assert verdict["good"] is False
    assert post.call_count == 2
    assert "response_format" in post.call_args_list[0].kwargs["json"]
    assert "response_format" not in post.call_args_list[1].kwargs["json"]
    assert len(post.call_args_list[1].kwargs["json"]["messages"][1]["content"]) == 2


@pytest.mark.parametrize("status_code", [400, 500])
def test_chat_completion_does_not_expose_provider_error_body(monkeypatch, status_code):
    response_body = "provider response containing private listing data"
    monkeypatch.setattr(
        ai.requests,
        "post",
        MagicMock(
            return_value=SimpleNamespace(status_code=status_code, text=response_body)
        ),
    )

    with pytest.raises(ai.AIEvaluationError) as exc_info:
        ai._chat_completion(
            "test-key", "https://provider.example/v1", "test-model", [], json_mode=True
        )

    assert str(exc_info.value) == (
        "AI provider rejected the request with HTTP 400"
        if status_code == 400
        else "AI provider returned HTTP 500"
    )
    assert response_body not in str(exc_info.value)


def test_download_image_rejects_non_591_hosts(monkeypatch):
    request = MagicMock()
    monkeypatch.setattr(ai.requests, "get", request)

    assert ai._download_image("https://example.com/image.jpg") is None
    request.assert_not_called()


def test_image_adapter_keeps_certificate_verification():
    adapter = ai._ImageHTTPAdapter()
    context = adapter._ssl_context()

    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


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
