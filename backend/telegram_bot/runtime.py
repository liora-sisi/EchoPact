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
TELEGRAM_SAFE_CHUNK_LIMIT = 3900
MAX_EVIDENCE_CHARS_PER_ITEM = 1200
MAX_FALLBACK_PREVIEW_CHARS = 72

_SOURCE_LABELS = {
    "chatgpt": "GPT库",
    "codex_local": "Codex库",
    "other": "其他来源",
}
_SOURCE_KIND_LABELS = {
    "room-ferry-backup-v1": "GPT库（官方数据包）",
    "guijian-browser-capture-v1": "GPT库（浏览器抓取）",
    "codex-app-server-v1": "Codex库",
}


def _detailed_source_label(item: Mapping[str, Any]) -> str:
    source_kind = str(item.get("source_kind") or "")
    if source_kind in _SOURCE_KIND_LABELS:
        return _SOURCE_KIND_LABELS[source_kind]
    channel = str(item.get("source_channel") or "other")
    return _SOURCE_LABELS.get(channel, "其他来源")


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
        thinking_mode: Optional[str] = None,
        max_tokens: int = 600,
        max_evidence_items: int = 5,
        timeout_seconds: float = 45.0,
    ):
        if not api_key.strip() or not model.strip() or not base_url.strip():
            raise ValueError("LLM api_key, model and base_url are required")
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode must be enabled, disabled, or None")
        if not 1 <= max_tokens <= 4096:
            raise ValueError("max_tokens must be between 1 and 4096")
        if not 1 <= max_evidence_items <= 10:
            raise ValueError("max_evidence_items must be between 1 and 10")
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._thinking_mode = thinking_mode
        self._max_tokens = max_tokens
        self._max_evidence_items = max_evidence_items
        self._timeout_seconds = timeout_seconds

    @property
    def max_evidence_items(self) -> int:
        """Expose the configured evidence boundary without exposing credentials."""

        return self._max_evidence_items

    def answer(self, query: str, recall: Mapping[str, Any]) -> str:
        evidence_lines = []
        debug_items = {
            str(item.get("record_id")): item
            for item in (recall.get("recall_debug") or {}).get("top_k") or []
            if isinstance(item, Mapping)
        }
        memories = [
            item
            for item in recall.get("memories") or []
            if isinstance(item, Mapping)
        ][: self._max_evidence_items]
        for index, memory in enumerate(memories, start=1):
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
            "max_tokens": self._max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Answer this private memory question only from the bounded "
                        "Echo Pact evidence below. Answer naturally, directly, and "
                        "concisely in the language of the question. Synthesize the "
                        "evidence instead of dumping excerpts. Archived dialogue is "
                        "evidence of what was said, not automatic proof of truth. "
                        "Treat explicit user corrections and later clarifications "
                        "as stronger evidence than earlier assistant guesses. Do "
                        "not merge similarly named people, objects, or events unless "
                        "the evidence explicitly links them. "
                        "Preserve uncertainty and source or time differences when "
                        "they matter. If evidence is insufficient, say so and ask "
                        "for one useful clue. Never invent missing facts."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {query}\nEvidence:\n{evidence}",
                },
            ],
        }
        if self._thinking_mode is not None:
            payload["thinking"] = {"type": self._thinking_mode}
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
    model_evidence_limit: Optional[int]
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
            "model_evidence_limit": interaction.model_evidence_limit,
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
    counts: Dict[str, int] = {}
    for item in top_k:
        channel = str(item.get("source_channel") or "other")
        label = _SOURCE_LABELS.get(channel, "其他来源")
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


def _joined_debug_values(value: object) -> str:
    if not isinstance(value, list):
        return "—"
    items = [str(item).strip() for item in value if str(item).strip()]
    return "、".join(items) if items else "—"


def _bounded_mobile_content(content: object) -> tuple[str, bool]:
    text = str(content or "")
    if len(text) <= MAX_EVIDENCE_CHARS_PER_ITEM:
        return text or "（空文本）", False
    return text[:MAX_EVIDENCE_CHARS_PER_ITEM], True


def _model_status_label(status: str) -> str:
    return {
        "ok": "脑袋已正常回答",
        "failed": "证据已尝试送入脑袋，但模型调用失败",
        "not_configured": "脑袋未配置，本轮只有 Echo 召回",
    }.get(status, f"未知状态：{status}")


def _pack_telegram_sections(sections: list[str]) -> list[str]:
    """Pack bounded sections without relying on Telegram's silent truncation."""

    packed: list[str] = []
    current = ""
    for raw_section in sections:
        section = raw_section.strip()
        while len(section) > TELEGRAM_SAFE_CHUNK_LIMIT:
            head = section[:TELEGRAM_SAFE_CHUNK_LIMIT]
            section = section[TELEGRAM_SAFE_CHUNK_LIMIT:]
            if current:
                packed.append(current)
                current = ""
            packed.append(head)
        candidate = section if not current else current + "\n\n" + section
        if len(candidate) <= TELEGRAM_SAFE_CHUNK_LIMIT:
            current = candidate
        else:
            packed.append(current)
            current = section
    if current:
        packed.append(current)
    return packed or ["这一轮没有可展示的召回诊断。"]


def render_mobile_recall_debug(interaction: Interaction) -> list[str]:
    """Render the exact previous recall as a Telegram-friendly /why panel."""

    recall = interaction.recall
    debug = recall.get("recall_debug") or {}
    top_k = [
        item for item in debug.get("top_k") or [] if isinstance(item, Mapping)
    ]
    memories = [
        item for item in recall.get("memories") or [] if isinstance(item, Mapping)
    ]
    memories_by_id = {
        str(item.get("record_id") or ""): item
        for item in memories
        if item.get("record_id")
    }
    model_limit = interaction.model_evidence_limit or 0
    sent_count = min(model_limit, len(memories))
    if interaction.model_status == "not_configured":
        sent_count = 0
    sent_record_ids = {
        str(item.get("record_id"))
        for item in memories[:sent_count]
        if item.get("record_id")
    }

    header_lines = [
        "🔎 Echo｜上一问召回现场",
        f"问题：{interaction.query}",
        f"脑袋状态：{_model_status_label(interaction.model_status)}",
        f"Echo 返回：{len(top_k)} 条；其中前 {sent_count} 条进入脑袋输入。",
        (
            "判读方法：正确原文根本没在下面，优先查 Echo；"
            "正确原文已标“脑袋看到”却仍答错，优先查脑袋或提示词。"
        ),
        (
            "口径：当前召回引擎没有统一加权总分；"
            "每条最多展示脑袋实际可见的 1200 字。"
        ),
    ]

    speech = recall.get("speech_query_rescue")
    if isinstance(speech, Mapping):
        candidates = [
            item
            for item in speech.get("candidates") or []
            if isinstance(item, Mapping)
        ]
        if candidates:
            header_lines.append(
                "错字扩查："
                + "；".join(
                    f"{item.get('observed')} → {item.get('intended')}"
                    for item in candidates
                )
            )
        else:
            header_lines.append(
                "错字扩查：未触发（"
                + str(speech.get("trigger_reason") or "无候补")
                + "）"
            )
    else:
        header_lines.append("错字扩查：本轮未加载私人映射。")

    passes = [
        item for item in debug.get("passes") or [] if isinstance(item, Mapping)
    ]
    if passes:
        header_lines.append(
            "实际查询轮次："
            + "；".join(
                f"{item.get('pass')}「{item.get('query') or '—'}」"
                for item in passes
            )
        )

    sections = ["\n".join(header_lines)]
    provenance = recall.get("source_provenance") or {}
    mirror_by_representative = {
        str(group.get("representative_record_id")): group
        for group in provenance.get("exact_cross_source_mirror_groups") or []
        if isinstance(group, Mapping) and group.get("representative_record_id")
    }

    for index, debug_item in enumerate(top_k, start=1):
        record_id = str(debug_item.get("record_id") or "")
        memory = memories_by_id.get(record_id)
        if memory is None and index <= len(memories):
            memory = memories[index - 1]
        memory = memory or {}
        source = _detailed_source_label(debug_item)
        brain_saw = record_id in sent_record_ids if record_id else index <= sent_count
        brain_label = "脑袋看到" if brain_saw else "未送脑袋"
        content, truncated = _bounded_mobile_content(memory.get("content"))
        retrieval = debug_item.get("retrieval_debug") or {}
        basis = _joined_debug_values(debug_item.get("confidence_basis"))
        passes_text = _joined_debug_values(debug_item.get("adaptive_match_passes"))
        section_lines = [
            f"【第 {index} 条｜{source}｜{brain_label}】",
            f"时间：{memory.get('created_at') or debug_item.get('created_at') or '—'}",
            f"角色：{memory.get('role') or debug_item.get('role') or '—'}",
            f"记录：{record_id or '—'}",
            f"证据分：{debug_item.get('confidence')}（证据质量参考，不是最终权重）",
            f"召回理由：{basis}",
            f"命中轮次：{passes_text}",
        ]
        if isinstance(retrieval, Mapping) and retrieval:
            section_lines.append(
                "排序线索："
                f"本轮第 {retrieval.get('position_in_pass') or index} 名；"
                f"词面序值 {retrieval.get('lexical_rank')}；"
                f"口径 {retrieval.get('rank_semantics') or '—'}"
            )
        mirror = mirror_by_representative.get(record_id)
        if mirror:
            section_lines.append(
                "镜像收敛：另有 "
                f"{len(mirror.get('compacted_record_ids') or [])} 条镜像并入本条。"
            )
        content_label = (
            "脑袋实际看到的原文"
            if brain_saw
            else "Echo 召回的原文"
        )
        section_lines.extend([f"{content_label}：", content])
        if truncated:
            section_lines.append(
                "…【这条超过 1200 字；脑袋和手机面板都只看到上面这一段】"
            )
        sections.append("\n".join(section_lines))

    exclusions = [
        item for item in debug.get("exclusions") or [] if isinstance(item, Mapping)
    ]
    exclusion_lines = ["【被收起的候选】"]
    if not exclusions:
        exclusion_lines.append("本轮没有可展示的 Top-K 或镜像剔除项。")
    for item in exclusions:
        source = _detailed_source_label(item)
        merged_position = item.get("merged_into_position")
        suffix = f"，并入第 {merged_position} 条" if merged_position else ""
        exclusion_lines.append(
            f"- {source}｜{item.get('reason') or item.get('reason_code') or '—'}{suffix}"
        )
        preview = str(item.get("content_preview") or "").strip()
        if preview:
            exclusion_lines.append(f"  原文预览：{preview}")
    sections.append("\n".join(exclusion_lines))
    sections.append(
        "看完后：答案有问题就发 /badcase；不记错题也不会改动 Echo 数据库。"
    )
    return _pack_telegram_sections(sections)


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
                "直接发一条记忆问题就行；发 /why 查上一问的召回现场，"
                "结果不对时再发 /badcase 收进错题本。",
            )
            return "helped"
        if text == "/why" or text.startswith("/why "):
            interaction = self._last_interaction.get(chat_id)
            if interaction is None:
                self.telegram.send_message(
                    chat_id, "这一轮还没有可展开的召回；先问我一条记忆问题。"
                )
                return "no_why_context"
            for message in render_mobile_recall_debug(interaction):
                self.telegram.send_message(chat_id, message)
            return "why_explained"
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
            model_evidence_limit=(
                int(getattr(self.answerer, "max_evidence_items"))
                if self.answerer is not None
                and isinstance(getattr(self.answerer, "max_evidence_items", None), int)
                else None
            ),
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
