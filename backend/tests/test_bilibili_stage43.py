from backend.app.bilibili import (
    AccessStrategy,
    BilibiliAccessConfig,
    BilibiliAccessMode,
    INITIAL_STATE_PATTERN,
    PLAYINFO_PATTERN,
    _append_player_subtitle_candidates,
    _build_strategy_plan,
    _extract_json_assignment,
    _html_payload_to_info,
    _player_subtitle_requests,
    _sign_wbi_params,
    _strategy_access_config,
)
from backend.app.bilibili_cookie import (
    cookie_header_to_httpx_cookies,
    simplify_bilibili_cookie_collection,
    simplify_bilibili_cookie_header,
)
from backend.app.browser_profile import (
    _store_login_window_cookie_result,
    _playwright_error_message,
    create_cookie_extract_session,
    extract_simplified_cookie_header_from_profile,
)
from backend.app.task_manager import TaskManager
from backend.app.utils import redact_secrets


def test_wbi_signing_adds_expected_fields():
    params = {"aid": 1, "cid": 2, "empty": ""}
    signed = _sign_wbi_params(params, "0123456789abcdef0123456789abcdef")

    assert signed["aid"] == 1
    assert signed["cid"] == 2
    assert "empty" not in signed
    assert isinstance(signed["wts"], int)
    assert len(signed["w_rid"]) == 32


def test_html_initial_state_and_playinfo_parse_without_audio_urls():
    html = '''
    <script>window.__INITIAL_STATE__={"videoData":{"aid":123,"bvid":"BV1abc123456","title":"Demo","pages":[{"page":1,"cid":456,"part":"P1","duration":7}]}};</script>
    <script>window.__playinfo__={"cid":456,"data":{"dash":{"audio":[{"baseUrl":"https://audio.example/a.m4s"}]},"subtitle":{"subtitles":[{"lan":"zh-CN","subtitle_url":"//subtitle.example/sub.json"}]}}};</script>
    '''
    initial_state = _extract_json_assignment(html, INITIAL_STATE_PATTERN)
    playinfo = _extract_json_assignment(html, PLAYINFO_PATTERN)
    info = _html_payload_to_info(type("Parsed", (), {"p_index": None, "bv_id": "BV1abc123456"})(), initial_state, playinfo)

    assert info["aid"] == 123
    assert info["cid"] == 456
    assert info["bvid"] == "BV1abc123456"
    assert "dash" not in str(info)
    assert info["subtitle"]["subtitles"][0]["subtitle_url"] == "//subtitle.example/sub.json"


def test_player_wbi_requests_match_page_script_shape_first():
    requests = _player_subtitle_requests("BV1abc123456", 123, 456, 789)
    wbi_request = next(item for item in requests if item[0] == "player_wbi_api")
    ep_request = next(item for item in requests if item[0] == "player_wbi_api_ep")

    assert wbi_request[3] == {"cid": 456, "aid": 123}
    assert ep_request[3] == {"cid": 456, "aid": 123, "ep_id": 789}


def test_log_redaction_removes_cookie_token_and_local_path():
    text = r"Cookie: SESSDATA=secret; bili_jct=token Authorization: Bearer abc.def.ghi C:\Users\me\runtime\browser-profile"
    redacted = redact_secrets(text)

    assert "secret" not in redacted
    assert "token" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "browser-profile" not in redacted


def test_log_redaction_removes_standalone_bilibili_cookie_fields():
    redacted = redact_secrets("SESSDATA=secret; bili_jct=token; other=value")

    assert "secret" not in redacted
    assert "token" not in redacted
    assert "other=value" in redacted


def test_simplify_cookie_header_removes_prefix_orders_fields_and_splits_first_equals():
    raw = (
        "Cookie: other=ignored; bili_ticket=ticket=value; SESSDATA=session; "
        "DedeUserID=123; bili_jct=jct; DedeUserID__ckMd5=md5; bili_ticket_expires=999"
    )

    assert simplify_bilibili_cookie_header(raw) == (
        "SESSDATA=session; bili_jct=jct; DedeUserID=123; "
        "DedeUserID__ckMd5=md5; bili_ticket=ticket=value; bili_ticket_expires=999"
    )


def test_simplify_cookie_header_skips_missing_fields():
    assert simplify_bilibili_cookie_header("bili_jct=jct; SESSDATA=session") == "SESSDATA=session; bili_jct=jct"


def test_simplify_cookie_collection_uses_same_whitelist_order():
    raw = [
        {"name": "DedeUserID", "value": "123"},
        {"name": "not_allowed", "value": "ignored"},
        {"name": "SESSDATA", "value": "session"},
        {"name": "bili_ticket_expires", "value": "999"},
    ]

    assert simplify_bilibili_cookie_collection(raw) == "SESSDATA=session; DedeUserID=123; bili_ticket_expires=999"


def test_cookie_header_to_httpx_cookies_uses_simplified_fields_only():
    cookies = cookie_header_to_httpx_cookies("foo=bar; SESSDATA=session; bili_jct=jct")
    cookie_names = {cookie.name for cookie in cookies.jar}

    assert cookie_names == {"SESSDATA", "bili_jct"}


def test_profile_busy_error_is_specific_and_sanitized():
    message = _playwright_error_message(
        Exception(
            "Failed to create a ProcessSingleton for your profile directory "
            r"C:\Users\me\Desktop\project\runtime\browser-profile"
        )
    )

    assert "占用" in message
    assert "C:\\" not in message
    assert "browser-profile" not in message


def test_login_window_cookie_result_is_read_from_memory_by_token():
    token = create_cookie_extract_session()

    class DummyContext:
        def cookies(self, urls):
            return [
                {"name": "SESSDATA", "value": "session", "domain": ".bilibili.com"},
                {"name": "bili_jct", "value": "jct", "domain": ".bilibili.com"},
                {"name": "DedeUserID", "value": "123", "domain": ".bilibili.com"},
                {"name": "not_allowed", "value": "ignored", "domain": ".bilibili.com"},
            ]

    _store_login_window_cookie_result(token, DummyContext(), "测试提取成功")
    payload = extract_simplified_cookie_header_from_profile(token)

    assert payload["cookie_header"] == "SESSDATA=session; bili_jct=jct; DedeUserID=123"
    assert payload["fields"] == ["SESSDATA", "bili_jct", "DedeUserID"]


def test_manual_cookie_strategy_does_not_prepend_other_modes():
    config = BilibiliAccessConfig(
        mode=BilibiliAccessMode.COOKIE_HEADER,
        cookie_header="SESSDATA=abc; bili_jct=def",
    )
    plan = _build_strategy_plan(config)

    assert [item.mode for item in plan] == [BilibiliAccessMode.COOKIE_HEADER]
    effective = _strategy_access_config(config, plan[0])
    assert effective.mode == BilibiliAccessMode.COOKIE_HEADER
    assert effective.cookie_header == "SESSDATA=abc; bili_jct=def"


def test_auto_strategy_skips_known_failed_probe_modes():
    plan = _build_strategy_plan(BilibiliAccessConfig())
    modes = [item.mode for item in plan]

    assert modes == [BilibiliAccessMode.BILIBILI_API, BilibiliAccessMode.LOCAL_BROWSER_PROFILE]
    assert BilibiliAccessMode.ANONYMOUS not in modes
    assert BilibiliAccessMode.ENHANCED_HEADERS not in modes
    assert BilibiliAccessMode.IMPERSONATE not in modes


def test_player_subtitle_candidates_must_match_current_aid_and_cid():
    candidates = []
    existing_urls = set()
    payload = {
        "data": {
            "subtitle": {
                "subtitles": [
                    {
                        "lan": "ai-zh",
                        "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/111222right.json",
                    },
                    {
                        "lan": "ai-zh",
                        "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/999888wrong.json",
                    },
                ],
            },
        },
    }

    _append_player_subtitle_candidates(
        payload,
        "player_api",
        candidates,
        existing_urls,
        aid=111,
        cid=222,
    )

    assert len(candidates) == 1
    assert "111222" in candidates[0].url


def test_deprecated_user_modes_are_coerced_to_manual_cookie():
    manager = TaskManager()

    assert manager._coerce_access_mode("anonymous") == BilibiliAccessMode.COOKIE_HEADER
    assert manager._coerce_access_mode("enhanced_headers") == BilibiliAccessMode.COOKIE_HEADER
    assert manager._coerce_access_mode("bilibili_api") == BilibiliAccessMode.COOKIE_HEADER
    assert manager._coerce_access_mode("impersonate") == BilibiliAccessMode.COOKIE_HEADER
    assert manager._coerce_access_mode("browser_cookie") == BilibiliAccessMode.COOKIE_HEADER
    assert manager._coerce_access_mode("cookies_file") == BilibiliAccessMode.COOKIE_HEADER
