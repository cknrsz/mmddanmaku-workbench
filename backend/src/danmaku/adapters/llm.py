import json
import re
import socket
import ssl
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from danmaku.application.classification.model import ModelFailure, PublishText
from danmaku.domain.analysis import ANALYSIS_TIMEOUT_SECONDS, ModelConfig, Operation

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 32 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024


def _transport_failure(error: httpx.RequestError, code: str) -> ModelFailure:
    pending: list[BaseException] = [error]
    seen: set[BaseException] = set()
    details: list[str] = []
    connect_codes = {
        ssl.SSLCertVerificationError: "llm_tls_certificate",
        ssl.SSLError: "llm_tls",
        socket.gaierror: "llm_dns",
        ConnectionRefusedError: "llm_connection_refused",
    }
    detected = set()
    while pending:
        cause = pending.pop()
        if cause in seen:
            continue
        seen.add(cause)
        detected.update(kind for kind in connect_codes if isinstance(cause, kind))
        if isinstance(cause, BaseExceptionGroup):
            pending.extend(reversed(cause.exceptions))
        if chained := (cause.__cause__ or cause.__context__):
            pending.append(chained)
        fields = [type(cause).__name__]
        for field in ("errno", "winerror", "verify_code"):
            value = getattr(cause, field, None)
            if type(value) is int:
                fields.append(f"{field}={value}")
        if isinstance(cause, ssl.SSLError) and cause.reason and re.fullmatch(r"[A-Z0-9_]+", cause.reason):
            fields.append(cause.reason)
        diagnostic = " ".join(fields)
        if diagnostic not in details:
            details.append(diagnostic)
    if isinstance(error, httpx.ConnectError):
        code = next((failure for kind, failure in connect_codes.items() if kind in detected), code)
    return ModelFailure(code, detail=" → ".join(details))


def _provider_error_detail(error: dict, api_key: str, instructions: str, data: dict) -> str | None:
    sensitive = {api_key, instructions}
    sensitive.update(line for line in instructions.splitlines() if line.strip())
    sensitive.update(
        entry["text"] for entries in data.values() if isinstance(entries, list)
        for entry in entries if isinstance(entry, dict) and isinstance(entry.get("text"), str)
    )
    if any(isinstance(value, str) and "\\" in value for value in error.values()):
        sensitive.update(
            json.dumps(text, ensure_ascii=ascii_only)[1:-1]
            for text in tuple(sensitive) for ascii_only in (False, True)
        )
    details = []
    for field in ("code", "type", "param", "message"):
        value = error.get(field)
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str):
            continue
        matches = sorted((text for text in sensitive if text and text in value), key=len, reverse=True)
        if matches:
            value = re.sub("|".join(map(re.escape, matches)), "［已隐藏］", value)
        value = value.encode("utf-8", errors="replace").decode("utf-8").strip()
        if value:
            details.append(f"{field}={value}")
    return "\n".join(details) or None


def _response_json(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ModelFailure("llm_invalid_response") from None


def _decode(
    status: int, body: bytes, api_key: str, instructions: str, data: dict, publish: PublishText | None,
) -> object:
    successful = 200 <= status < 300
    if successful:
        envelope = _response_json(body)
    else:
        try:
            envelope = json.loads(body)
        except (ValueError, RecursionError):
            envelope = None
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if not successful or isinstance(error, dict):
        error = error if isinstance(error, dict) else {}
        message = error.get("message")
        message = message.lower() if isinstance(message, str) else ""
        code = "llm_request_rejected"
        if status in {401, 403}:
            code = "llm_credential_rejected"
        elif status == 429:
            code = "llm_rate_limited"
        elif status == 408 or status >= 500:
            code = "llm_unavailable"
        elif (status == 413 or error.get("code") in ("context_length_exceeded", "context_window_exceeded")
              or "maximum context length" in message or "maximum context window" in message):
            code = "llm_context_limit"
        detail = None
        if error and len(body) <= MAX_ERROR_BYTES:
            detail = _provider_error_detail(error, api_key, instructions, data)
        raise ModelFailure(code, http_status=status, detail=detail)
    try:
        choice = envelope["choices"][0]
        if choice["finish_reason"] == "length":
            raise ModelFailure("llm_context_limit")
        if choice["finish_reason"] != "stop":
            raise ModelFailure("llm_invalid_response")
        message = choice["message"]
        if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
            raise ModelFailure("llm_invalid_response")
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError
        encoded = content.encode("utf-8")
        if publish is not None:
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                publish("reasoning", reasoning.encode("utf-8", errors="replace").decode("utf-8"))
            publish("output", content)
        return _response_json(encoded)
    except (TypeError, AttributeError, IndexError, KeyError, UnicodeError):
        raise ModelFailure("llm_invalid_response") from None


async def _stream_events(response: httpx.Response) -> AsyncIterator[bytes]:
    """Decode DeepSeek's SSE data fields with bounded line and response sizes."""
    pending = b""
    fields: list[bytes] = []
    size = 0
    event_size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_STREAM_BYTES:
            raise ModelFailure("llm_context_limit")
        *lines, pending = (pending + chunk).split(b"\n")
        for raw_line in lines:
            line = raw_line.removesuffix(b"\r")
            if not line:
                if fields:
                    yield b"\n".join(fields)
                    fields.clear()
                    event_size = 0
            elif line.startswith(b"data:"):
                field = line[5:].removeprefix(b" ")
                event_size += len(field) + 1
                if event_size > MAX_RESPONSE_BYTES:
                    raise ModelFailure("llm_context_limit")
                fields.append(field)
        if len(pending) > MAX_RESPONSE_BYTES:
            raise ModelFailure("llm_context_limit")


async def _decode_stream(response: httpx.Response, publish: PublishText) -> object:
    output: list[str] = []
    size = 0
    finished = False
    try:
        async for event in _stream_events(response):
            if event == b"[DONE]":
                if not finished:
                    break
                return _response_json("".join(output).encode("utf-8"))
            frame = _response_json(event)
            choices = frame["choices"]
            if choices == []:
                continue
            if finished or len(choices) != 1:
                break
            choice = choices[0]
            delta = choice["delta"]
            if any(delta.get(field) for field in ("tool_calls", "function_call", "refusal")):
                break
            for field, section in (("reasoning_content", "reasoning"), ("content", "output")):
                text = delta.get(field)
                if text is None or text == "":
                    continue
                if not isinstance(text, str):
                    raise TypeError
                size += len(text.encode("utf-8"))
                if size > MAX_RESPONSE_BYTES:
                    raise ModelFailure("llm_context_limit")
                publish(section, text)
                if section == "output":
                    output.append(text)
            reason = choice.get("finish_reason")
            if reason == "length":
                raise ModelFailure("llm_context_limit")
            if reason not in (None, "stop"):
                break
            finished = reason == "stop"
    except (TypeError, AttributeError, IndexError, KeyError, UnicodeError):
        raise ModelFailure("llm_invalid_response") from None
    raise ModelFailure("llm_invalid_response")


class ChatCompletions:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def generate(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict,
    ) -> object:
        return await self._generate(config, secret, instructions, data, None)

    async def generate_stream(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict, publish: PublishText,
    ) -> object:
        return await self._generate(config, secret, instructions, data, publish)

    async def _generate(
        self, config: ModelConfig, secret: SecretStr | None, instructions: str, data: dict, publish: PublishText | None,
    ) -> object:
        assert config.base_url is not None and secret is not None
        api_key = secret.get_secret_value()
        body = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
        }
        if config.uses_deepseek_thinking:
            body["thinking"] = {"type": "enabled" if config.thinking_enabled else "disabled"}
            if config.thinking_enabled:
                body["reasoning_effort"] = config.reasoning_effort
            if publish is not None:
                body["stream"] = True
        try:
            async with self.client.stream(
                "POST", f"{config.base_url}/chat/completions", follow_redirects=False,
                timeout=httpx.Timeout(ANALYSIS_TIMEOUT_SECONDS, connect=10),
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            ) as response:
                status = response.status_code
                response_body = bytearray()
                successful = 200 <= status < 300
                if (successful and publish is not None and
                        response.headers.get("content-type", "").split(";", 1)[0].strip() == "text/event-stream"):
                    return await _decode_stream(response, publish)
                limit = MAX_RESPONSE_BYTES if successful else MAX_ERROR_BYTES
                try:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        response_body.extend(chunk)
                        if len(response_body) > limit:
                            if successful:
                                raise ModelFailure("llm_context_limit")
                            response_body.clear()
                            break
                except httpx.RequestError:
                    if successful:
                        raise
                return _decode(status, bytes(response_body), api_key, instructions, data, publish)
        except httpx.ConnectTimeout as exc:
            raise _transport_failure(exc, "llm_connect_timeout") from None
        except httpx.ConnectError as exc:
            raise _transport_failure(exc, "llm_network") from None
        except (TimeoutError, httpx.TimeoutException):
            raise ModelFailure("llm_timeout") from None
        except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError) as exc:
            raise _transport_failure(exc, "llm_connection_interrupted") from None
        except httpx.DecodingError:
            raise ModelFailure("llm_invalid_response") from None
        except httpx.RequestError as exc:
            raise _transport_failure(exc, "llm_network") from None
        except httpx.InvalidURL:
            raise ModelFailure("llm_request_rejected") from None


class SyntheticModel:
    """Deterministic offline responses for exercising the workflow."""

    async def generate(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict,
    ) -> object:
        cues = data["chinese_subtitles"] or data["japanese_subtitles"]
        return {"items": [self._classify(item, cues) for item in data["items"]]}

    async def generate_stream(
        self, config: ModelConfig, secret: SecretStr | None, operation: Operation,
        instructions: str, data: dict, publish: PublishText,
    ) -> object:
        result = await self.generate(config, secret, operation, instructions, data)
        publish("output", json.dumps(result, ensure_ascii=False, indent=2))
        return result

    @staticmethod
    def _classify(item: dict, cues: list[dict]) -> dict:
        nearby = [cue for cue in cues if cue["start_ms"] - 3000 <= item["time_ms"] <= cue["end_ms"] + 3000]
        text = item["text"].strip(" 。！？!?")
        match = next((cue for cue in nearby if text == cue["text"].strip(" 。！？!?")), None)
        return {
            "id": item["id"], "category": "translation" if match else "other",
            "reason": "与同期字幕一致" if match else "合成规则未找到一致的同期字幕",
            "cue_ids": [match["id"]] if match else [],
            "evidence": "sufficient",
        }
