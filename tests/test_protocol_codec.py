"""协议编解码单元测试（外壳、md5psk、脱敏、base64、分片、请求构造）。

全部为**离线**测试：不连接真实奥维。官方样例/手册夹具见
``ovital_mcp/mock/fixtures/``，每条都带 ``_source``。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ovital_mcp.protocol import codec, constants as C
from ovital_mcp.protocol.errors import ProtocolDecodeError

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "ovital_mcp" / "mock" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 请求外壳
# ---------------------------------------------------------------------------


def test_encode_request_envelope_matches_manual():
    frame = codec.encode_request(20006, {})
    data = json.loads(frame)
    assert data == {"cmdid": 20006, "ret": 0, "msg": {}}
    # 手册 2.1.3.6/2.3.1.1：请求一律 ret=0
    assert data["ret"] == C.REQUEST_RET


def test_encode_request_keeps_utf8_chinese():
    frame = codec.encode_request(20112, {"Name": "集水区草稿"})
    assert "集水区草稿" in frame  # 官方样例 JSON.stringify 不转义中文
    assert json.loads(frame)["msg"]["Name"] == "集水区草稿"


def test_encode_request_rejects_bad_arguments():
    with pytest.raises(ProtocolDecodeError):
        codec.encode_request("20006", {})  # type: ignore[arg-type]
    with pytest.raises(ProtocolDecodeError):
        codec.encode_request(20006, ["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 响应解码
# ---------------------------------------------------------------------------


def test_decode_success_without_ret_defaults_zero():
    response = codec.decode_message('{"cmdid":20006,"msg":{}}')
    assert response.cmdid == 20006
    assert response.ret == 0
    assert response.msg == {}
    assert response.raw["cmdid"] == 20006


def test_decode_ret_version_number_is_not_error():
    response = codec.decode_message(json.dumps(load_fixture("20000-login-success.json")))
    # 手册 2.3.1.2.1：成功时 ret 是版本号（817），绝不能当失败
    assert response.ret == 817
    assert response.msg["AppId"] == 41


def test_decode_event_20015():
    response = codec.decode_message(json.dumps(load_fixture("20015-object-changed-event.json")))
    assert response.cmdid == C.CMD_OBJ_CHANGED
    assert response.msg["Flag"] == 1
    assert response.msg["ListID"] == [1307408641, 1836892915]


def test_decode_bytes_frame():
    response = codec.decode_message('{"cmdid":20006,"msg":{}}'.encode())
    assert response.cmdid == 20006


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[1,2,3]",
        '{"msg":{}}',
        '{"cmdid":"abc","msg":{}}',
        '{"cmdid":20006,"ret":"abc","msg":{}}',
    ],
)
def test_decode_invalid_frames_raise_with_details(raw: str):
    with pytest.raises(ProtocolDecodeError):
        codec.decode_message(raw)


def test_decode_non_object_msg_is_wrapped_not_lost():
    response = codec.decode_message('{"cmdid":20008,"msg":"Omap is Closed!"}')
    assert response.msg == {"_raw": "Omap is Closed!"}


# ---------------------------------------------------------------------------
# 登录摘要（手册 2.3.1.1 示例 md5psk=E10ADC3949BA59AB 对应明文 123456）
# ---------------------------------------------------------------------------


def test_md5psk_matches_manual_example():
    # 手册 2.3.1.1 示例：md5psk "E10ADC3949BA59AB"（MD5 大写前 16 位）
    assert codec.md5psk("123456") == "E10ADC3949BA59AB"


def test_md5psk_length_and_case():
    value = codec.md5psk("test-psk-not-secret")
    assert len(value) == C.MD5PSK_LENGTH
    assert value == value.upper()


def test_md5psk_rejects_empty():
    with pytest.raises(ProtocolDecodeError):
        codec.md5psk("")


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


def test_redact_msg_hides_sensitive_fields():
    msg = {"md5psk": "ADAC03FC5EBD6F43", "Psk": "secret", "Other": "keep"}
    redacted = codec.redact_msg(msg)
    assert redacted["md5psk"] == "***"
    assert redacted["Psk"] == "***"
    assert redacted["Other"] == "keep"


def test_redact_text_masks_hex_digests_and_secrets():
    text = "md5psk=ADAC03FC5EBD6F43 psk=test-psk-not-secret"
    redacted = codec.redact_text(text, ["test-psk-not-secret"])
    assert "ADAC03FC5EBD6F43" not in redacted
    assert "test-psk-not-secret" not in redacted


def test_safe_msg_summary_never_contains_secret():
    summary = codec.safe_msg_summary(
        {"md5psk": "ADAC03FC5EBD6F43", "name": "x"},
        secrets=["ADAC03FC5EBD6F43"],
    )
    assert "ADAC03FC5EBD6F43" not in summary
    assert "***" in summary


# ---------------------------------------------------------------------------
# base64（20301 Data / 分片 FragmentData）
# ---------------------------------------------------------------------------


def test_b64_roundtrip_standard():
    payload = b"\x89PNG\r\n\x1a\nhello"
    assert codec.b64decode_tolerant(codec.b64encode(payload)) == payload


def test_b64decode_tolerant_urlsafe_and_sdk_mapping():
    payload = b"\xfb\xff\xfe binary"
    standard = codec.b64encode(payload)
    urlsafe = standard.replace("+", "-").replace("/", "_")
    assert codec.b64decode_tolerant(urlsafe) == payload
    # 官方样例 Base64ToStr 的映射与 URL-safe 相反：'-'->'/'、'_'->'+'
    # 该映射与 URL-safe 存在歧义，因此用 variants 断言「其中一种变体解得原文」，
    # 并说明歧义必须靠后续 JSON 解析校验（docs/protocol-notes.md 待实测）。
    sdk_mapping = standard.replace("+", "_").replace("/", "-")
    decoded_values = [value for _, value in codec.b64decode_variants(sdk_mapping)]
    assert payload in decoded_values
    assert codec.b64decode_tolerant(standard) == payload


def test_b64decode_tolerant_rejects_garbage():
    with pytest.raises(ProtocolDecodeError):
        codec.b64decode_tolerant("!!!not base64!!!")


# ---------------------------------------------------------------------------
# 分片（官方样例有、手册未写）
# ---------------------------------------------------------------------------


def test_split_text_and_fragment_accumulator_out_of_order():
    text = '{"cmdid":20110,"msg":{"ObjItems":[]}}'
    parts = codec.split_text(text, 7)
    assert "".join(parts) == text
    accumulator = codec.FragmentAccumulator()
    encoded = [
        {C.FRAGMENT_NUM_FIELD: len(parts), C.FRAGMENT_INDEX_FIELD: i, C.FRAGMENT_DATA_FIELD: codec.b64encode(part.encode())}
        for i, part in enumerate(parts)
    ]
    # 乱序喂入，最后收齐
    result = None
    for fragment in reversed(encoded):
        result = accumulator.add(fragment)
    assert result == text


def test_fragment_accumulator_rejects_bad_index():
    accumulator = codec.FragmentAccumulator()
    with pytest.raises(ProtocolDecodeError):
        accumulator.add(
            {C.FRAGMENT_NUM_FIELD: 2, C.FRAGMENT_INDEX_FIELD: 5, C.FRAGMENT_DATA_FIELD: "e30="}
        )


def test_encode_fragment_requests_20112():
    frames = codec.encode_fragment_requests(C.CMD_WRITE_OBJECT_TREE, {"ObjTree": {"x": 1}}, 10)
    assert len(frames) >= 2
    decoded = [json.loads(frame) for frame in frames]
    assert [item["msg"][C.FRAGMENT_INDEX_FIELD] for item in decoded] == list(range(len(frames)))
    assert all(item["msg"][C.FRAGMENT_NUM_FIELD] == len(frames) for item in decoded)
    assembled = "".join(item["msg"][C.FRAGMENT_DATA_FIELD] for item in decoded)
    assert json.loads(assembled) == {"ObjTree": {"x": 1}}


def test_is_fragment_message():
    assert codec.is_fragment_message(
        {C.FRAGMENT_NUM_FIELD: 1, C.FRAGMENT_INDEX_FIELD: 0, C.FRAGMENT_DATA_FIELD: ""}
    )
    assert not codec.is_fragment_message({"ObjItems": []})


# ---------------------------------------------------------------------------
# 请求体构造（字段出处见各 docstring 与 docs/protocol-notes.md）
# ---------------------------------------------------------------------------


def test_build_obj_request_defaults_cgcs2000():
    msg = codec.build_obj_request(738224536)
    assert msg == {"ObjID": 738224536, "LatlngType": 1}


def test_build_selected_request_validates_flag():
    assert codec.build_selected_request(1) == {"Flag": 1}
    with pytest.raises(ProtocolDecodeError):
        codec.build_selected_request(9)


def test_build_search_request_sends_both_documented_field_names():
    msg = codec.build_search_request(name="天桥", isrh_opt=5)
    # 手册 2.3.22.1 表写 idParent/idShapeSelect，示例写 ParentID/ShapeSelect → 两套都发
    assert msg["ParentID"] == 0 and msg["idParent"] == 0
    assert msg["ShapeSelect"] == 0 and msg["idShapeSelect"] == 0
    assert msg["SrhTxt"] == "天桥"
    assert msg["SrhOpt"] == 5
    with pytest.raises(ProtocolDecodeError):
        codec.build_search_request(name="x", isrh_opt=99)


def test_build_delete_request_uses_manual_field_name():
    msg = codec.build_delete_request([1682750257, "2014305939"])
    assert msg == {"NoGroup": 1, "ListID": [1682750257, 2014305939]}
    with pytest.raises(ProtocolDecodeError):
        codec.build_delete_request([])


def test_build_map_data_request_sends_both_level_field_names():
    msg = codec.build_map_data_request(
        bbox_lonlat=(106.55, 38.99, 106.60, 39.03), max_level=12, max_pixel=1_000_000
    )
    item = msg["ReqMapDatas"][0]
    # 手册 2.3.29.1 表写 MaxLevel，官方样例 BuildReqMapData 发 MapLevel → 两个都发（待实测）
    assert item["MaxLevel"] == 12 and item["MapLevel"] == 12
    assert (item["x_lng1"], item["y_lat1"]) == (106.55, 38.99)
    assert (item["x_lng2"], item["y_lat2"]) == (106.60, 39.03)
    assert item["CoordType"] == 0 and item["MaxPixel"] == 1_000_000
    with pytest.raises(ProtocolDecodeError):
        codec.build_map_data_request(
            bbox_lonlat=(106.60, 38.99, 106.55, 39.03), max_level=12, max_pixel=100
        )


def test_build_contour_request_sends_both_array_names():
    msg = codec.build_contour_request(bbox_lonlat=(106.55, 38.99, 106.60, 39.03))
    assert "ReqMapDatas" in msg and "ReqContours" in msg
    item = msg["ReqContours"][0]
    assert item["ElevValue"] == 30 and item["CheckFlag"] == 0


# ---------------------------------------------------------------------------
# 官方夹具完整性：每条样例都必须带 _source（手册章节 + URL）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_every_fixture_has_official_source(path: pathlib.Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    source = data.get("_source")
    assert isinstance(source, dict), f"{path.name} 缺少 _source"
    assert source.get("manual_section"), f"{path.name} 缺少 manual_section"
    assert source.get("manual_url", "").startswith("https://"), f"{path.name} 缺少官方 URL"
    assert source.get("verified_against_real_device") is False
    assert data.get("cmdid") in C.CMD_NAMES or data.get("cmdid") is not None


def test_all_fixture_requests_are_encodable():
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("_kind") != "request":
            continue
        frame = codec.encode_request(data["cmdid"], data.get("msg", {}))
        decoded = json.loads(frame)
        assert decoded["cmdid"] == data["cmdid"]
        assert decoded["ret"] == 0
