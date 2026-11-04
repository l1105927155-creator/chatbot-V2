"""Safe model failure codes and user facing recovery notices."""

try:
    from .model_selection import model_route
except ImportError:  # Support contract tests loading plugin modules directly.
    from model_selection import model_route


BACKUP_ROUTES = (("wb", "space-bunny"), ("wb", "step-5-preview"))

# Stable public codes shared by the Python host and native DSH recovery hook.
MODEL_FAILURE_CODES = frozenset({
    "MODEL_TIMEOUT", "MODEL_TRANSPORT_ERROR", "RATE_LIMITED",
    "MODEL_UNAVAILABLE", "QUOTA_EXCEEDED", "MISSING_CREDENTIAL",
    "INVALID_CREDENTIAL", "INVALID_REQUEST", "CONTEXT_WINDOW_EXCEEDED",
    "EMPTY_RESPONSE", "MODEL_ERROR", "MODEL_OUTPUT_LIMIT", "MAX_TOKENS",
    "MEMORY_CAPACITY_INSUFFICIENT", "MEMORY_REQUEST_TOO_LARGE",
    "MEMORY_CAPACITY_CHANGED", "UNKNOWN",
    "MEMORY_FACT_REVIEW_INVALID_DECISION", "MEMORY_FACT_REVIEW_CAPACITY",
    "MEMORY_SOURCE_INVALID",
})

RECOVERABLE_API_FAILURE_CODES = frozenset({
    "MODEL_TIMEOUT", "MODEL_TRANSPORT_ERROR", "RATE_LIMITED",
    "MODEL_UNAVAILABLE", "EMPTY_RESPONSE", "MODEL_ERROR",
})

_CODE_ALIASES = {
    "AUTH": "INVALID_CREDENTIAL",
    "FORBIDDEN": "INVALID_CREDENTIAL",
    "INVALID_API_KEY": "INVALID_CREDENTIAL",
    "RATE_LIMIT": "RATE_LIMITED",
    "SERVER": "MODEL_UNAVAILABLE",
    "TIMEOUT": "MODEL_TIMEOUT",
    "TRANSPORT": "MODEL_TRANSPORT_ERROR",
    "QUOTA": "QUOTA_EXCEEDED",
    "ACCOUNT_QUOTA": "QUOTA_EXCEEDED",
    "UNAUTHORIZED": "INVALID_CREDENTIAL",
}


def normalize_model_failure_code(value):
    """Return a stable allowlisted code; never return provider error text."""
    if not isinstance(value, str):
        return "MODEL_ERROR"
    code = value.strip().upper()
    code = _CODE_ALIASES.get(code, code)
    return code if code in MODEL_FAILURE_CODES else "MODEL_ERROR"


def _status(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise ValueError("invalid provider status")
    return value


def validate_recovery(value, *, requested_route=None, actual_route=None):
    """Validate a bounded recovery receipt against the configured backup route."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid recovery summary")
    required = {"requested_provider", "requested_model", "provider", "model",
                "code", "attempted", "recovered"}
    if not required <= value.keys() or value.keys() - (required | {"status"}):
        raise ValueError("invalid recovery summary fields")
    try:
        requested = model_route((value["requested_provider"], value["requested_model"]))
        actual = model_route((value["provider"], value["model"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery route") from exc
    if requested == actual or actual not in BACKUP_ROUTES:
        raise ValueError("recovery route is not the authorized backup")
    if requested_route is not None and requested != model_route(requested_route):
        raise ValueError("requested recovery route mismatch")
    if actual_route is not None and actual != model_route(actual_route):
        raise ValueError("actual recovery route mismatch")
    if value["attempted"] is not True or not isinstance(value["recovered"], bool):
        raise ValueError("invalid recovery state")
    raw_code = value["code"]
    if not isinstance(raw_code, str):
        raise ValueError("invalid recovery code")
    code = normalize_model_failure_code(raw_code)
    if raw_code.strip().upper() not in RECOVERABLE_API_FAILURE_CODES | _CODE_ALIASES.keys():
        raise ValueError("recovery code is not allowlisted")
    if code not in RECOVERABLE_API_FAILURE_CODES:
        raise ValueError("recovery code is not a recoverable model API failure")
    result = {"requested_provider": requested[0], "requested_model": requested[1],
              "provider": actual[0], "model": actual[1], "code": code,
              "attempted": True, "recovered": value["recovered"]}
    status = _status(value.get("status"))
    if status is not None:
        result["status"] = status
    return result


def recovery_notice(summary, *, subject=""):
    summary = validate_recovery(summary)
    if summary is None or summary["recovered"] is not True:
        return ""
    prefix = f"{subject}：" if subject else ""
    return prefix + "主模型接口暂时未能响应，已临时切换到备用模型完成处理。主模型设置保持不变。"


def model_failure_notice(code, recovery=None, *, subject=""):
    summary = validate_recovery(recovery)
    prefix = f"{subject}：" if subject else ""
    raw_code = code.strip().upper() if isinstance(code, str) else ""
    normalized = normalize_model_failure_code(code)
    if normalized == "CONTEXT_WINDOW_EXCEEDED":
        detail = "模型上下文已满，本轮未完成。请用 /new 开始新会话，或继续发送新请求。"
    elif normalized == "QUOTA_EXCEEDED":
        detail = "模型服务当前额度不足，本轮未完成。请联系 Owner 检查服务额度。"
    elif normalized in {"MISSING_CREDENTIAL", "INVALID_CREDENTIAL"}:
        detail = "模型服务凭据未配置或已失效，本轮未完成。请 Owner 检查服务配置。"
    elif normalized in {"MEMORY_CAPACITY_INSUFFICIENT", "MEMORY_REQUEST_TOO_LARGE"}:
        if summary and not summary["recovered"]:
            detail = "主模型请求失败后，备用模型容量不足，本次记忆整理没有写入。请缩小整理范围后重试。"
        else:
            detail = "本次记忆整理内容超过可处理容量，没有写入。请缩小整理范围后重试。"
    elif normalized == "MEMORY_CAPACITY_CHANGED":
        detail = "主模型请求失败后，备用模型容量检查未能完成，本次记忆整理没有写入。请稍后重试。"
    elif normalized in {"MODEL_OUTPUT_LIMIT", "MAX_TOKENS"}:
        if summary and not summary["recovered"]:
            detail = "主模型请求失败后，备用模型输出达到长度上限，本次处理未完成。请缩小整理范围后重试。"
        else:
            detail = "模型输出达到长度上限，本次处理未完成。请缩小整理范围后重试。"
    elif normalized == "INVALID_REQUEST":
        detail = "模型接口未能完成这次请求；本轮未完成，可以继续发送新请求。"
    elif raw_code.startswith("MEMORY_"):
        detail = "记忆整理的来源或内容校验未通过，本次候选已拒绝写入；原消息保留，正常聊天可以继续。"
    elif normalized == "EMPTY_RESPONSE":
        if summary and not summary["recovered"]:
            detail = "主模型请求失败后，备用模型没有返回可用内容，本轮未完成。请稍后重试。"
        else:
            detail = "模型接口没有返回可用内容，本轮未完成；可以稍后重试或继续发送新请求。"
    elif summary and not summary["recovered"] and normalized in RECOVERABLE_API_FAILURE_CODES:
        detail = "模型接口暂不可用，主线路和备用线路均未完成。请稍后重试。"
    elif summary and not summary["recovered"]:
        if normalized == "MODEL_ERROR":
            detail = "模型主线路异常后，备用线路恢复未能完成，本轮已结束。请稍后重试。"
        else:
            detail = "主模型请求失败后，备用模型未能完成本次处理。本轮未完成，请稍后重试。"
    else:
        detail = "模型接口暂时未能完成请求，本轮已结束；可以稍后重试或继续发送新请求。"
    return prefix + detail
