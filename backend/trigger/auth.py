"""Echo Pact 触发服务访问鉴权（fail-closed）。

规则：
- ACCESS_CODE 未配置、为空或仍是示例占位符：所有受保护接口返回 503；
- 缺少或错误的 Bearer：统一 401，并带 WWW-Authenticate: Bearer；
- 访问码比较使用 hmac.compare_digest 恒定时间比较；
- 任何日志、异常和测试输出都不得打印访问码。
"""
import hmac
import os
from typing import Optional

from fastapi import HTTPException, Request

# 常见示例占位符，命中即视为“未配置”
PLACEHOLDER_CODES = frozenset({
    "your_access_code_here",
    "changeme",
    "change_me",
    "change-me",
    "placeholder",
    "access_code",
    "password",
})


def get_access_code() -> Optional[str]:
    """返回有效的 ACCESS_CODE；未配置/为空/占位符时返回 None。"""
    code = os.getenv("ACCESS_CODE", "").strip()
    if not code or code.lower() in PLACEHOLDER_CODES:
        return None
    return code


# ---------- M5-04：身份上下文解析（所有召回端点的唯一身份来源） ----------
#
# 规则（v3 终审定案）：
# - 网络身份只来自 Bearer 认证上下文，body/query 里的 agent_id 一律只是
#   兼容性断言，绝不作为身份来源；
# - legacy ACCESS_CODE 命中 → agt-legacy（兼容存量部署；新装不配 ACCESS_CODE
#   即默认关闭；也可用 ECHO_DISABLE_LEGACY_CODE=1 强制关闭）；
# - cred_id.secret → backend.memory.identity.verify_credential 派生 agent_id；
# - ACCESS_CODE 未配置且库中无任何凭证 → 503（fail-closed 语义不变）；
# - 其余一切失败 → 统一 401 + WWW-Authenticate: Bearer；无效 cred_id 与
#   错误 secret 在 identity 层已用 dummy KDF 拉平时间侧信道；
# - 访问码/凭证 secret 不进日志、不进响应。

_LEGACY_DISABLE_ENV = "ECHO_DISABLE_LEGACY_CODE"


def _legacy_code_agent_enabled() -> bool:
    return os.getenv(_LEGACY_DISABLE_ENV, "").strip().lower() not in (
        "1", "true", "yes"
    )


def _has_any_credential() -> bool:
    """库中是否已签发过任何凭证；库不存在/表不存在一律视为无。"""
    try:
        from ..memory.records_v1 import _connect, _resolve_db_path

        conn = _connect(_resolve_db_path(None))
        try:
            conn.execute("PRAGMA query_only = ON")
            row = conn.execute(
                "SELECT 1 FROM agent_credentials LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return ""


def resolve_principal(request: Request) -> str:
    """FastAPI 依赖：v1 端点的身份上下文，返回派生的 agent_id。"""
    from ..memory.identity import LEGACY_PRINCIPAL, verify_credential

    token = _bearer_token(request)
    code = get_access_code()

    # 1) legacy 访问码兼容路径（恒定时间比较）
    if (
        code is not None
        and token
        and _legacy_code_agent_enabled()
        and hmac.compare_digest(token.encode("utf-8"), code.encode("utf-8"))
    ):
        return LEGACY_PRINCIPAL

    # 2) 凭证路径：cred_id.secret（无效 cred_id 也走 dummy KDF，见 identity）
    if token and "." in token:
        agent_id = verify_credential(token)
        if agent_id is not None:
            return agent_id

    # 3) fail-closed：两种认证方式都未配置 → 503（不透露配置细节）
    if code is None and not _has_any_credential():
        raise HTTPException(
            status_code=503,
            detail="Service unavailable: access control is not configured.",
        )

    # 4) 其余失败统一 401，不区分“无效 cred_id”与“错误 secret”
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def assert_agent_id_compat(body_agent_id: Optional[str], principal: str) -> str:
    """body agent_id 兼容断言：只校验，不产生身份。

    - 缺省 → 直接采用认证 principal；
    - "default" → 仅作为 legacy principal 的历史别名；
    - 空白串 → 422（客户端显式传了无效值）；
    - 其余必须与认证派生身份一致，否则 403；
    - 返回值永远是认证上下文派生的 principal。
    """
    from ..memory.identity import LEGACY_PRINCIPAL

    if body_agent_id is None:
        return principal
    if body_agent_id == "default":
        effective = LEGACY_PRINCIPAL
    elif not body_agent_id.strip():
        raise HTTPException(status_code=422, detail="agent_id 不能为空")
    else:
        effective = body_agent_id
    if effective != principal:
        raise HTTPException(
            status_code=403, detail="agent_id 与认证身份不一致"
        )
    return principal
