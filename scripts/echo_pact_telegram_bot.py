#!/usr/bin/env python3
"""Run the text-only Echo Pact Telegram bot with an in-process loopback API."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
import threading
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.memory.recall_debug import load_speech_query_policy  # noqa: E402
from backend.telegram_bot.readonly_api import (  # noqa: E402
    ReadonlyApiConfig,
    create_readonly_app,
)
from backend.telegram_bot.runtime import (  # noqa: E402
    BadCaseStore,
    EchoTelegramBot,
    LocalRecallClient,
    OpenAICompatibleAnswerer,
    TelegramBotApi,
)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _optional_answerer() -> OpenAICompatibleAnswerer | None:
    api_key = os.getenv("ECHO_LLM_API_KEY", "").strip()
    model = os.getenv("ECHO_LLM_MODEL", "").strip()
    base_url = os.getenv("ECHO_LLM_BASE_URL", "").strip()
    configured = [bool(api_key), bool(model), bool(base_url)]
    if not any(configured):
        return None
    if not all(configured):
        raise RuntimeError(
            "ECHO_LLM_API_KEY, ECHO_LLM_MODEL and ECHO_LLM_BASE_URL "
            "must be configured together"
        )
    thinking_mode = os.getenv("ECHO_LLM_THINKING_MODE", "").strip().lower() or None
    max_tokens = int(os.getenv("ECHO_LLM_MAX_TOKENS", "600"))
    return OpenAICompatibleAnswerer(
        api_key=api_key,
        model=model,
        base_url=base_url,
        thinking_mode=thinking_mode,
        max_tokens=max_tokens,
    )


def main() -> int:
    db_path = _required_env("ECHO_TELEGRAM_DB_PATH")
    agent_id = _required_env("ECHO_TELEGRAM_AGENT_ID")
    bot_token = _required_env("ECHO_TELEGRAM_BOT_TOKEN")
    allowed_user_id = int(_required_env("ECHO_TELEGRAM_ALLOWED_USER_ID"))
    bad_case_dir = _required_env("ECHO_TELEGRAM_BAD_CASE_DIR")
    port = int(os.getenv("ECHO_TELEGRAM_LOCAL_API_PORT", "8765"))
    if not 1024 <= port <= 65535:
        raise RuntimeError("ECHO_TELEGRAM_LOCAL_API_PORT must be 1024..65535")
    policy_path = os.getenv("ECHO_TELEGRAM_SPEECH_POLICY", "").strip()
    policy = load_speech_query_policy(policy_path) if policy_path else None
    local_access_token = secrets.token_urlsafe(32)

    app = create_readonly_app(
        ReadonlyApiConfig(
            db_path=db_path,
            agent_id=agent_id,
            result_limit=5,
            expected_source_channels=("chatgpt", "codex_local"),
            speech_query_policy=policy,
            local_access_token=local_access_token,
        )
    )

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server_thread = threading.Thread(
        target=server.run,
        name="echo-pact-readonly-api",
        daemon=True,
    )
    server_thread.start()
    for _ in range(200):
        if server.started:
            break
        if not server_thread.is_alive():
            raise RuntimeError("local read-only API stopped during startup")
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("local read-only API did not start")

    bot = EchoTelegramBot(
        allowed_user_id=allowed_user_id,
        telegram=TelegramBotApi(bot_token),
        recall_client=LocalRecallClient(
            f"http://127.0.0.1:{port}",
            access_token=local_access_token,
        ),
        bad_cases=BadCaseStore(
            bad_case_dir,
            forbidden_root=str(REPO_ROOT),
        ),
        answerer=_optional_answerer(),
    )
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
