"""Text-only, whitelist-only Telegram runtime for local Echo Pact recall."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Mapping, Optional, Protocol
from urllib import request


BAD_CASE_SCHEMA_VERSION = "echo-pact-bad-case-v1"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_EVIDENCE_CHARS_PER_ITEM = 1200
MAX_FALLBACK_PREVIEW_CHARS = 72


class RecallClient(Protocol):
    def recall(self, query: str) -> Dict[str, Any]: ...


class Answerer(Protocol):
    def answer(self, query: str, recall: Mapping[str, Any]) -> str: ...


class TelegramTransport(Protocol):
    def get_updates(self, *, offset: Optional[int]) -> list[Dict[str, Any]]: ...

    def send_message(self, chat_id: int, text: str) -> None: ...


class LocalRecallClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

    def recall(self, query: str) -> Dict[str, Any]:
        payload = json.dumps({"query": query}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["X-Echo-Local-Token"] = self.access_token
        req = request.Request(
            self.base_url + "/recall-debug",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except Exception:
            raise RuntimeError("local read-only recall API failed") from None
        if not isinstance(decoded, dict):
            raise RuntimeError("local read-only recall API returned invalid data")
        return decoded


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        api_root: str = "https://api.telegram.org",
        timeout_seconds: float = 40.0,
    ):
        if not token.strip():
            raise ValueError("Telegram bot token is required")
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def _call(self, method: str, payload: Mapping[str, Any]) -> Any:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self._api_root}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except Exception:
            # Never retain the token-bearing URL as an exception cause.
            raise RuntimeError("Telegram Bot API request failed") from None
        if not isinstance(decoded, Mapping) or decoded.get("ok") is not True:
            raise RuntimeError("Telegram Bot API returned an error")
        return decoded.get("result")

    def get_updates(self, *, offset: Optional[int]) -> list[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "timeout": 25,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return [dict(item) for item in result] if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:TELEGRAM_MESSAGE_LIMIT],
                "disable_web_page_preview": True,
            },
        )


class OpenAICompatibleAnswerer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 45.0,
    ):
        if not api_key.strip() or not model.strip() or not base_url.strip():
            raise ValueError("LLM api_key, model and base_url are required")
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._timeout_seconds = timeout_seconds

    def answer(self, query: str, recall: Mapping[str, Any]) -> str:
        evidence_lines = []
        debug_items = {
            str(item.get("record_id")): item
            for item in (recall.get("recall_debug") or {}).get("top_k") or []
            if isinstance(item, Mapping)
        }
        for index, memory in enumerate(recall.get("memories") or [], start=1):
            if not isinstance(memory, Mapping):
                continue
            record_id = str(memory.get("record_id") or "")
            source = (debug_items.get(record_id) or {}).get(
                "source_channel", "other"
            )
            content = str(memory.get("content") or "")[:MAX_EVIDENCE_CHARS_PER_ITEM]
            evidence_lines.append(
                f"[{index}] source={source}; created_at={memory.get('created_at')}; "
                f"verified={bool(memory.get('verified'))}; content={content}"
            )
        evidence = "\n".join(evidence_lines) or "[no recalled evidence]"
        payload = {
            "model": self._model,
            "temperature": 0.2,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You answer a private memory question only from the "
                        "bounded Echo Pact evidence below. Keep uncertainty and "
                        "source boundaries explicit. Do not invent missing facts. "
                        "Answer in the language used by the question."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {query}\nEvidence:\n{evidence}",
                },
            ],
        }
        req = request.Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            text = decoded["choices"][0]["message"]["content"]
        except Exception:
            # Do not surface the endpoint, key, provider body, or evidence.
            raise RuntimeError("language model response failed") from None
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("language model returned no text")
        return text.strip()


@dataclass(frozen=True)
class Interaction:
    update_id: int
    chat_id: int
    user_id: int
    query: str
    answer: str
    model_status: str
    recall: Dict[str, Any]


class BadCaseStore:
    def __init__(self, root: str, *, forbidden_root: Optional[str] = None):
        self.root = Path(root).expanduser().resolve()
        project_root = Path(forbidden_root).resolve() if forbidden_root else None
        if project_root is not None and (
            self.root == project_root or project_root in self.root.parents
        ):
            raise ValueError("bad-case storage must stay outside the repository")

    def save(self, interaction: Interaction, *, note: str = "") -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        captured_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(
            f"{interaction.update_id}\n{interaction.chat_id}\n{interaction.query}".encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        final_path = self.root / (
            f"bad-case-update-{interaction.update_id}-{digest}.json"
        )
        if final_path.exists():
            return final_path
        temp_path = self.root / f".{final_path.name}.{os.getpid()}.tmp"
        packet = {
            "schema_version": BAD_CASE_SCHEMA_VERSION,
            "captured_at": captured_at,
            "note": note.strip(),
            "dialogue": [
                {"role": "user", "content": interaction.query},
                {"role": "assistant", "content": interaction.answer},
            ],
            "telegram": {
                "update_id": interaction.update_id,
                "chat_id": interaction.chat_id,
                "user_id": interaction.user_id,
            },
            "model_status": interaction.model_status,
            "recall": {
                key: interaction.recall.get(key)
                for key in (
                    "query",
                    "recall_mode",
                    "coverage",
                    "memories",
                    "source_provenance",
                    "speech_query_rescue",
                    "recall_debug",
                )
                if key in interaction.recall
            },
        }
        try:
            temp_path.write_text(
                json.dumps(packet, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, final_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return final_path


def _fallback_answer(recall: Mapping[str, Any]) -> str:
    memories = [
        item for item in recall.get("memories") or [] if isinstance(item, Mapping)
    ]
    if not memories:
        return "Echo这次没有召回到可用记忆，而且嘴也被捂住了；我没有假装找到。"
    labels = []
    for label, memory in zip(("A", "B"), memories[:2]):
        content = " ".join(str(memory.get("content") or "").split())
        if len(content) > MAX_FALLBACK_PREVIEW_CHARS:
            content = content[: MAX_FALLBACK_PREVIEW_CHARS - 3] + "..."
        labels.append(f"{label}「{content}」")
    joined = "和".join(labels)
    return f"记忆召回了{joined}，但嘴被捂住了。"


def _diagnostic_footer(recall: Mapping[str, Any]) -> str:
    debug = recall.get("recall_debug") or {}
    top_k = [
        item for item in debug.get("top_k") or [] if isinstance(item, Mapping)
    ]
    labels = {
        "chatgpt": "GPT库",
        "codex_local": "Codex库",
        "other": "其他来源",
    }
    counts: Dict[str, int] = {}
    for item in top_k:
        channel = str(item.get("source_channel") or "other")
        label = labels.get(channel, "其他来源")
        counts[label] = counts.get(label, 0) + 1
    source_summary = "、".join(
        f"{label}{counts[label]}条"
        for label in ("GPT库", "Codex库", "其他来源")
        if label in counts
    ) or "没有结果"
    speech = recall.get("speech_query_rescue") or {}
    candidates = (
        speech.get("candidates") or [] if isinstance(speech, Mapping) else []
    )
    query_summary = (
        f"错字候补扩查{len(candidates)}条" if candidates else "原问直接查"
    )
    exclusions = [
        item
        for item in debug.get("exclusions") or []
        if isinstance(item, Mapping)
        and item.get("reason_code") in {"result_limit", "exact_mirror_compacted"}
    ]
    excluded_summary = f"｜另有{len(exclusions)}条收起" if exclusions else ""
    return f"【召回说明】{source_summary}｜{query_summary}{excluded_summary}"


class EchoTelegramBot:
    def __init__(
        self,
        *,
        allowed_user_id: int,
        telegram: TelegramTransport,
        recall_client: RecallClient,
        bad_cases: BadCaseStore,
        answerer: Optional[Answerer] = None,
    ):
        if allowed_user_id <= 0:
            raise ValueError("allowed_user_id must be positive")
        self.allowed_user_id = allowed_user_id
        self.telegram = telegram
        self.recall_client = recall_client
        self.bad_cases = bad_cases
        self.answerer = answerer
        self._offset: Optional[int] = None
        self._last_interaction: Dict[int, Interaction] = {}

    def _authorized_message(
        self, update: Mapping[str, Any]
    ) -> Optional[tuple[int, int, str]]:
        message = update.get("message")
        if not isinstance(message, Mapping):
            return None
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, Mapping) or not isinstance(chat, Mapping):
            return None
        try:
            user_id = int(sender.get("id"))
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return None
        if (
            user_id != self.allowed_user_id
            or chat_id != self.allowed_user_id
            or chat.get("type") != "private"
        ):
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return user_id, chat_id, text.strip()

    def handle_update(self, update: Mapping[str, Any]) -> str:
        authorized = self._authorized_message(update)
        if authorized is None:
            return "ignored"
        user_id, chat_id, text = authorized
        update_id = int(update.get("update_id", 0))
        if text == "/start":
            self.telegram.send_message(
                chat_id,
                "直接发一条记忆问题就行；结果不对时再发 /badcase 收进本地错题本。",
            )
            return "helped"
        if text.startswith("/badcase"):
            interaction = self._last_interaction.get(chat_id)
            if interaction is None:
                self.telegram.send_message(
                    chat_id, "这一轮还没有可收进错题本的召回。"
                )
                return "no_bad_case"
            note = text[len("/badcase") :].strip()
            try:
                saved = self.bad_cases.save(interaction, note=note)
            except OSError:
                self.telegram.send_message(
                    chat_id, "这道错题暂时没能写进本地；原数据库没有受到影响。"
                )
                return "bad_case_failed"
            self.telegram.send_message(
                chat_id, f"这道错题已经收进本地：{saved.stem}"
            )
            return "bad_case_saved"

        try:
            recall = self.recall_client.recall(text)
        except Exception:
            self.telegram.send_message(
                chat_id, "Echo本地召回这次出错了；我没有假装找到记忆。"
            )
            return "recall_failed"

        model_status = "not_configured"
        answer = _fallback_answer(recall)
        if self.answerer is not None:
            try:
                answer = self.answerer.answer(text, recall)
                model_status = "ok"
            except Exception:
                model_status = "failed"
                answer = _fallback_answer(recall)
        footer = _diagnostic_footer(recall)
        max_answer_chars = max(0, TELEGRAM_MESSAGE_LIMIT - len(footer) - 2)
        answer = answer[:max_answer_chars] + "\n\n" + footer
        self.telegram.send_message(chat_id, answer)
        self._last_interaction[chat_id] = Interaction(
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            query=text,
            answer=answer,
            model_status=model_status,
            recall=dict(recall),
        )
        return "answered"

    def poll_once(self) -> int:
        updates = self.telegram.get_updates(offset=self._offset)
        processed = 0
        for update in updates:
            update_id = int(update.get("update_id", 0))
            self.handle_update(update)
            self._offset = max(self._offset or 0, update_id + 1)
            processed += 1
        return processed

    def run_forever(self) -> None:
        consecutive_failures = 0
        while True:
            try:
                self.poll_once()
                consecutive_failures = 0
            except KeyboardInterrupt:
                raise
            except RuntimeError:
                consecutive_failures += 1
                time.sleep(min(30, 2 ** min(consecutive_failures, 4)))
