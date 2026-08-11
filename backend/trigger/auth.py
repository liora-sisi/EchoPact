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


def require_access_code(request: Request) -> None:
    """FastAPI 依赖：挂载在 router 级别，保护其下全部端点。"""
    code = get_access_code()
    if code is None:
        # 不透露任何关于访问码配置的细节，也不记录访问码本身
        raise HTTPException(
            status_code=503,
            detail="Service unavailable: access control is not configured.",
        )
    authorization = request.headers.get("Authorization", "")
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):].strip()
    if not token or not hmac.compare_digest(
        token.encode("utf-8"), code.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
