"""Text-only workflow generation through a configured model provider.

This adapter never runs an agent, sends tool definitions, executes model output,
or writes credentials. API errors are intentionally summarized without response
bodies or request headers. Tests can supply an HTTPX client factory.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from easel.openclaw_workspace import config_path


class WorkflowModelError(RuntimeError):
    """Generation failed; no fabricated or agent fallback output is available."""


class WorkflowModelConfigError(ValueError):
    """Local model settings or environment credentials are not usable."""


@dataclass(frozen=True)
class _Settings:
    provider: str
    model: str
    base_url: str
    api: str
    key: str = field(repr=False)


@dataclass(frozen=True)
class _RequestBudget:
    max_tokens: int
    timeout: float
    effort: str | None
    disable_thinking: bool = False


def _env_value(name: str, seen: set[str] | None = None) -> str:
    """Resolve plain environment references only; no files, shell or eval."""
    visited = set() if seen is None else set(seen)
    if name in visited:
        return ""
    visited.add(name)
    value = os.environ.get(name, "").strip()
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if match:
        return _env_value(match.group(1), visited)
    if "${" in value or "\r" in value or "\n" in value:
        return ""
    return value


def _referenced_key(value: Any) -> str:
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        return _env_value(match.group(1)) if match else ""
    if isinstance(value, dict) and value.get("source") == "env":
        name = value.get("id")
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return _env_value(name)
    return ""


def _safe_base(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port  # Validate even though the value is not otherwise used.
        del port
    except ValueError:
        raise WorkflowModelConfigError("模型 Base URL 格式无效。") from None
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"https", "http"} or "${" in value):
        raise WorkflowModelConfigError("模型 Base URL 必须是无凭证和查询参数的 HTTP(S) 地址。")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise WorkflowModelConfigError("远程模型连接需要 HTTPS；HTTP 仅限本机地址。")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _endpoint(settings: _Settings) -> str:
    base = settings.base_url.rstrip("/")
    if settings.api == "anthropic-messages":
        if base.endswith("/messages"):
            return base
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    if base.endswith("/chat/completions"):
        return base
    return base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")


class WorkflowModel:
    def __init__(self, config_file: Path | None = None, *, timeout: float = 240,
                 client_factory: Callable[..., httpx.AsyncClient] | None = None):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("模型超时必须在 0 到 300 秒之间。")
        self.config_file = Path(config_file) if config_file is not None else None
        self.timeout = float(timeout)
        self.client_factory = client_factory

    def _settings(self) -> _Settings:
        path = self.config_file if self.config_file is not None else config_path()
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raise WorkflowModelConfigError("无法读取本机 OpenClaw 模型配置，请检查配置文件。") from None
            if not isinstance(data, dict):
                raise WorkflowModelConfigError("本机 OpenClaw 模型配置格式无效。")
        models = data.get("models", {})
        providers = models.get("providers", {}) if isinstance(models, dict) else {}
        providers = providers if isinstance(providers, dict) else {}
        agents = data.get("agents", {})
        defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
        primary = defaults.get("model", {}) if isinstance(defaults, dict) else {}
        primary = primary.get("primary", "") if isinstance(primary, dict) else primary
        primary = primary if isinstance(primary, str) else ""
        override_model = _env_value("EASEL_WORKFLOW_MODEL")
        provider_name = "minimax"
        model = ""
        if override_model:
            if "/" in override_model:
                prefix, _, candidate = override_model.partition("/")
                if prefix in providers or prefix.lower().startswith("minimax"):
                    provider_name, model = prefix, candidate
                else:
                    # Unregistered OpenAI-compatible model IDs may contain '/'.
                    model = override_model
            else:
                model = override_model
        elif "/" in primary and primary.partition("/")[0].lower().startswith("minimax"):
            provider_name, _, model = primary.partition("/")
        provider = providers.get(provider_name, {})
        provider = provider if isinstance(provider, dict) else {}
        if not model:
            candidates = provider.get("models", [])
            if isinstance(candidates, list):
                model = next((item.get("id", "") for item in candidates
                              if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]), "")
        if not model or len(model) > 240 or any(char in model for char in "\r\n"):
            raise WorkflowModelConfigError("未配置工作流模型，请配置 MiniMax 或 EASEL_WORKFLOW_MODEL。")
        override_base = _env_value("EASEL_WORKFLOW_BASE_URL")
        raw_base = override_base or provider.get("baseUrl")
        if not isinstance(raw_base, str) or not raw_base.strip():
            raise WorkflowModelConfigError("未配置工作流模型地址，请设置 EASEL_WORKFLOW_BASE_URL。")
        base = _safe_base(raw_base)
        override_api = _env_value("EASEL_WORKFLOW_API")
        if override_api:
            api = override_api
        elif override_base:
            api = "anthropic-messages" if ("/anthropic" in urlsplit(base).path or base.endswith("/messages")) else "openai-completions"
        else:
            api = provider.get("api") or ("anthropic-messages" if "/anthropic" in urlsplit(base).path else "openai-completions")
        if api not in {"anthropic-messages", "openai-completions"}:
            raise WorkflowModelConfigError("此工作流仅支持 anthropic-messages 或 openai-completions 文本接口。")
        # Explicit override first. The existing MiniMax env key is preferred to
        # provider env aliases; literal apiKey values in JSON are never consumed.
        key = _env_value("EASEL_WORKFLOW_KEY")
        previous_base = provider.get("baseUrl")
        if override_base and not key and isinstance(previous_base, str):
            previous_origin = urlsplit(_safe_base(previous_base))
            next_origin = urlsplit(base)
            if (previous_origin.scheme, previous_origin.netloc) != (next_origin.scheme, next_origin.netloc):
                raise WorkflowModelConfigError("模型地址已切换，请显式配置 EASEL_WORKFLOW_KEY，避免向新服务发送原服务密钥。")
        if not key and provider_name.lower().startswith("minimax"):
            key = _env_value("MINIMAX_API_KEY")
        key = key or _referenced_key(provider.get("apiKey"))
        if not key:
            raise WorkflowModelConfigError("缺少可用的模型环境密钥：请配置 EASEL_WORKFLOW_KEY 或 MINIMAX_API_KEY。")
        return _Settings(provider_name, model, base, api, key)

    def describe(self) -> dict[str, str]:
        """Safe diagnostic metadata; never return the key or its source value."""
        settings = self._settings()
        return {"provider": settings.provider, "model": settings.model,
                "base_url": settings.base_url, "api": settings.api}

    def _budget(self, settings: _Settings, task: str, max_tokens: int | None,
                effort: str | None) -> _RequestBudget:
        if task not in {"default", "script", "short_json"}:
            raise ValueError("未知模型任务预算；可选 default、script 或 short_json。")
        if max_tokens is None:
            max_tokens = 2048 if task == "short_json" else 8192
        if type(max_tokens) is not int or not 256 <= max_tokens <= 32768:
            raise ValueError("本工作流生成预算须为 256 到 32768 之间的整数。")
        if effort is not None and effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("思考档位仅接受 low、medium、high、xhigh 或 max；不支持 none。")
        # MiniMax's official contract, checked 2026-09-29:
        # https://platform.minimax.cn/docs/api-reference/text-anthropic-api
        # https://platform.minimax.cn/docs/api-reference/text-openai-api
        # M3.1 defaults to max effort and cannot disable thinking. M3 can disable
        # it; M2 cannot. Do not guess Claude budget_tokens or other providers'
        # reasoning controls from their Anthropic/OpenAI-compatible wire format.
        flash = settings.model == "MiniMax-M3.1-Flash-Preview"
        if effort is not None and not flash:
            raise WorkflowModelConfigError("当前已验证的思考档位仅适用于 MiniMax-M3.1-Flash-Preview。")
        if task == "short_json" and flash and effort is None:
            effort = "low"
        return _RequestBudget(
            max_tokens=max_tokens,
            timeout=min(self.timeout, 60) if task == "short_json" else self.timeout,
            effort=effort,
            disable_thinking=task == "short_json" and settings.model == "MiniMax-M3",
        )

    @staticmethod
    def _extract(payload: Any, settings: _Settings) -> str:
        if not isinstance(payload, dict) or payload.get("type") == "error" or payload.get("error"):
            raise WorkflowModelError("模型返回错误，未生成可用内容。")
        base_resp = payload.get("base_resp")
        if isinstance(base_resp, dict) and base_resp.get("status_code", 0) != 0:
            raise WorkflowModelError("模型服务拒绝了本次生成，请检查额度和模型权限。")
        if settings.api == "anthropic-messages":
            if payload.get("stop_reason") == "max_tokens":
                raise WorkflowModelError("模型生成达到长度上限（包含思考消耗），内容未完成；请调整本节点预算或简化要求。")
            blocks = payload.get("content", [])
            if not isinstance(blocks, list):
                raise WorkflowModelError("模型返回的文本结构无效。")
            if payload.get("stop_reason") == "tool_use" or any(isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks):
                raise WorkflowModelError("模型请求了工具调用；此节点只接受文本产出。")
            text = "\n".join(block["text"] for block in blocks
                             if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
        else:
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise WorkflowModelError("模型未返回有效文本结果。")
            choice = choices[0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise WorkflowModelError("模型输出被截断或拦截，不能当作完成结果。")
            message = choice.get("message", {})
            if not isinstance(message, dict):
                raise WorkflowModelError("模型返回的文本结构无效。")
            if message.get("tool_calls") or message.get("function_call") or choice.get("finish_reason") in {"tool_calls", "function_call"}:
                raise WorkflowModelError("模型请求了工具调用；此节点只接受文本产出。")
            text = message.get("content", "")
            if isinstance(text, list):
                text = "\n".join(block["text"] for block in text
                                 if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
        if not isinstance(text, str):
            raise WorkflowModelError("模型未返回有效文本结果。")
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        if not text or "<think>" in text:
            raise WorkflowModelError("模型只返回空内容或未完成的思考，未生成可用产物。")
        # A provider may echo the credential in a malformed answer. Never pass it
        # to persisted node results, even though it was not part of the prompt.
        return text.replace(settings.key, "[redacted]")

    async def _request(self, prompt: str, system: str, settings: _Settings, budget: _RequestBudget) -> str:
        headers = {"Content-Type": "application/json"}
        payload: dict[str, Any] = {"model": settings.model, "max_tokens": budget.max_tokens, "stream": False}
        if budget.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if settings.api == "anthropic-messages":
            if budget.effort is not None:
                payload["output_config"] = {"effort": budget.effort}
            headers.update({"x-api-key": settings.key, "anthropic-version": "2023-06-01"})
            payload["messages"] = [{"role": "user", "content": prompt}]
            if system:
                payload["system"] = system
        else:
            if budget.effort is not None:
                payload["reasoning_effort"] = budget.effort
            headers["Authorization"] = "Bearer " + settings.key
            payload["messages"] = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        options: dict[str, Any] = {
            "timeout": httpx.Timeout(budget.timeout, connect=min(20, budget.timeout)),
            "follow_redirects": False,
        }
        proxy = _env_value("EASEL_WORKFLOW_PROXY") or _env_value("EASEL_PROXY")
        if not proxy and urlsplit(settings.base_url).hostname == "api.minimax.io":
            proxy = "http://127.0.0.1:7890"
        if proxy:
            options["proxy"] = proxy
        factory = self.client_factory or httpx.AsyncClient
        try:
            async with factory(**options) as client:
                response = await client.post(_endpoint(settings), headers=headers, json=payload)
                if response.status_code < 200 or response.status_code >= 300:
                    raise WorkflowModelError(f"模型接口 HTTP {response.status_code}，请检查连接、额度和权限。")
                try:
                    result = response.json()
                except ValueError:
                    raise WorkflowModelError("模型接口未返回合法 JSON。") from None
        except httpx.TimeoutException:
            raise WorkflowModelError("模型生成超时，请稍后重试本节点。") from None
        except httpx.HTTPError:
            raise WorkflowModelError("模型连接失败，请检查本机网络和代理设置。") from None
        return self._extract(result, settings)

    async def generate(self, prompt: str, system: str = "", cancel: asyncio.Event | None = None,
                       *, task: str = "default", max_tokens: int | None = None,
                       effort: str | None = None) -> str:
        """Generate once; short_json bounds parameter extraction, not long scripts.

        default/script keep the existing 8192-token, provider-default reasoning
        behavior. short_json uses 2048 tokens and at most 60 seconds; for known
        MiniMax models it uses the officially supported low-latency controls.
        The caller still validates JSON. No automatic retry or model fallback.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("模型输入不能为空。")
        if not isinstance(system, str):
            raise ValueError("模型系统提示词必须是文本。")
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        settings = self._settings()
        budget = self._budget(settings, task, max_tokens, effort)
        request = asyncio.create_task(self._request(prompt, system, settings, budget))
        stop = asyncio.create_task(cancel.wait()) if cancel is not None else None
        tasks = {request, stop} if stop is not None else {request}
        try:
            done, _ = await asyncio.wait(tasks, timeout=budget.timeout, return_when=asyncio.FIRST_COMPLETED)
            if stop is not None and stop in done:
                raise asyncio.CancelledError
            if request not in done:
                raise WorkflowModelError("模型生成超时，请稍后重试本节点。")
            return request.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def generate(prompt: str, system: str = "", cancel: asyncio.Event | None = None,
                   *, task: str = "default", max_tokens: int | None = None,
                   effort: str | None = None) -> str:
    return await WorkflowModel().generate(prompt, system, cancel, task=task, max_tokens=max_tokens, effort=effort)
