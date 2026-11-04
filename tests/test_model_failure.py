import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "astrbot-plugins/v2_dsh_router"))
import model_failure as failure


@pytest.mark.parametrize(("raw", "expected"), [
    ("RATE_LIMIT", "RATE_LIMITED"),
    ("SERVER", "MODEL_UNAVAILABLE"),
    ("TIMEOUT", "MODEL_TIMEOUT"),
    ("TRANSPORT", "MODEL_TRANSPORT_ERROR"),
    ("ACCOUNT_QUOTA", "QUOTA_EXCEEDED"),
    ("invalid_request", "INVALID_REQUEST"),
    ("SECRET provider response", "MODEL_ERROR"),
    (None, "MODEL_ERROR"),
])
def test_failure_code_is_normalized_and_provider_text_is_not_exposed(raw, expected):
    assert failure.normalize_model_failure_code(raw) == expected


def test_valid_successful_recovery_summary_is_canonical_and_safe():
    summary = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "SERVER",
        "attempted": True, "recovered": True, "status": 503,
    }
    validated = failure.validate_recovery(
        summary, requested_route=("wb", "deepseek-v4.1-flash"),
        actual_route=("wb", "space-bunny"),
    )
    assert validated["code"] == "MODEL_UNAVAILABLE"
    assert validated["status"] == 503
    assert "备用模型完成处理" in failure.recovery_notice(validated)
    assert "deepseek-v4.1-flash" not in failure.recovery_notice(validated)


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(provider="other"),
    lambda x: x.update(model="glm-5.3"),
    lambda x: x.update(requested_model="space-bunny"),
    lambda x: x.update(attempted=1),
    lambda x: x.update(recovered="true"),
    lambda x: x.update(status=True),
    lambda x: x.update(status=99),
    lambda x: x.update(code="SECRET provider error"),
    lambda x: x.update(exception="provider body"),
])
def test_invalid_recovery_summaries_are_rejected(mutate):
    value = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": False,
    }
    mutate(value)
    with pytest.raises(ValueError):
        failure.validate_recovery(value)


def test_unrecovered_summary_says_both_routes_failed_only_with_receipt():
    value = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": False,
    }
    assert "主线路和备用线路均未完成" in failure.model_failure_notice("MODEL_TIMEOUT", value)
    assert "主线路和备用线路均未完成" not in failure.model_failure_notice("MODEL_TIMEOUT")


@pytest.mark.parametrize("code", [
    "QUOTA_EXCEEDED", "MISSING_CREDENTIAL", "INVALID_CREDENTIAL",
    "INVALID_REQUEST", "CONTEXT_WINDOW_EXCEEDED",
    "MEMORY_SOURCE_INVALID", "MEMORY_FACT_REVIEW_INVALID_DECISION",
    "MEMORY_CAPACITY_INSUFFICIENT", "MEMORY_REQUEST_TOO_LARGE",
    "MODEL_OUTPUT_LIMIT", "MAX_TOKENS",
])
def test_nonrecoverable_codes_cannot_authorize_a_backup_receipt(code):
    value = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": code,
        "attempted": True, "recovered": False,
    }
    with pytest.raises(ValueError):
        failure.validate_recovery(value)


def test_recovered_earlier_request_does_not_mask_final_failure():
    value = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": True,
    }
    notice = failure.model_failure_notice("INVALID_REQUEST", value)
    assert "备用模型完成处理" not in notice
    assert "接口未能完成" in notice and "未完成" in notice


@pytest.mark.parametrize("code", ["MEMORY_CAPACITY_INSUFFICIENT", "MEMORY_REQUEST_TOO_LARGE"])
def test_memory_capacity_failures_have_specific_non_api_notice(code):
    notice = failure.model_failure_notice(code)
    assert "超过可处理容量" in notice
    assert "线路" not in notice
    assert "模型接口" not in notice


def test_failed_primary_plus_backup_capacity_guard_does_not_claim_both_apis_failed():
    recovery = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": False,
    }
    notice = failure.model_failure_notice("MEMORY_CAPACITY_INSUFFICIENT", recovery)
    assert "备用模型容量不足" in notice
    assert "两条" not in notice and "线路均未完成" not in notice
    assert "接口暂不可用" not in notice


def test_unknown_fallback_preflight_failure_does_not_claim_api_outage():
    recovery = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": False,
    }
    notice = failure.model_failure_notice("UNKNOWN", recovery)
    assert "备用模型未能完成本次处理" in notice
    assert "接口暂不可用" not in notice


@pytest.mark.parametrize("code", ["MEMORY_CAPACITY_CHANGED", "UNKNOWN"])
def test_other_fallback_preflight_failures_do_not_claim_both_api_routes_failed(code):
    recovery = {
        "requested_provider": "wb", "requested_model": "deepseek-v4.1-flash",
        "provider": "wb", "model": "space-bunny", "code": "TIMEOUT",
        "attempted": True, "recovered": False,
    }
    notice = failure.model_failure_notice(code, recovery)
    assert "接口暂不可用" not in notice
    assert "未完成" in notice or "没有写入" in notice


@pytest.mark.parametrize("code", ["MODEL_OUTPUT_LIMIT", "MAX_TOKENS"])
def test_model_output_limit_has_specific_notice(code):
    assert "输出达到长度上限" in failure.model_failure_notice(code)


def test_memory_evidence_errors_do_not_claim_model_route_failure():
    text = failure.model_failure_notice("MEMORY_FACT_REVIEW_INVALID_DECISION")
    assert "来源或内容校验" in text
    assert "线路" not in text
