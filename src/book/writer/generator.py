"""Writer Generator——调用 LLM 生成叙事正文。"""

from __future__ import annotations
import httpx
import json
from typing import Optional, Generator

from ..config import config
from .prompt import build_user_prompt


class LLMClient:
    """通用 LLM 调用客户端。"""

    def __init__(
        self,
        api_base: str = "",
        api_key: str = "",
        model: str = "",
    ):
        self.api_base = api_base or config.llm_api_base
        self.api_key = api_key or config.llm_api_key
        self.model = model or config.llm_model

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.8,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> str:
        """发送 Chat Completion 请求，返回文本。"""
        if not self.api_key:
            return self._fallback_response(user_prompt)

        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=120.0)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[LLM] 调用失败: {type(e).__name__}")
            return self._fallback_response(user_prompt)

    def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.8,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> Generator[tuple[str, str], None, None]:
        """流式 Chat Completion，逐块 yield (kind, text)，kind 为 'thinking' 或 'content'。"""
        if not self.api_key:
            yield ("content", self._fallback_response(user_prompt))
            return

        url = f"{self.api_base.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": "high",
            "stream": True,
        }
        if response_format:
            payload["response_format"] = response_format

        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=120.0) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            reasoning = delta.get("reasoning_content", "")
                            if reasoning:
                                yield ("thinking", reasoning)
                            elif content:
                                yield ("content", content)
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except Exception as e:
            print(f"[LLM] 流式调用失败: {type(e).__name__}")
            yield ("content", self._fallback_response(user_prompt))

    def complete(self, prompt: str, temperature: float = 0.3, max_tokens: int = 1024,
                 response_format: dict | None = None) -> str:
        """发送简单补全请求（用于摘要、整合等非叙事任务）。"""
        return self.chat(
            system_prompt="你是一个精确的系统助手。请严格按指令输出，不要额外解释。",
            user_prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    @staticmethod
    def _fallback_response(user_prompt: str) -> str:
        return f"[未配置 API Key，无法生成内容]\n输入：{user_prompt[:100]}..."


class Writer:
    """叙事生成器——纯创作，不处理格式。"""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm or LLMClient()

    def generate(
        self,
        system_prompt: str,
        context: str,
        user_input: str,
    ) -> str:
        """
        生成叙事正文。

        Args:
            system_prompt: 作家身份的系统提示词（已包含故事设定）
            context: 当前故事状态 + 记忆 + NPC + 最近剧情
            user_input: 玩家当前行动
        Returns:
            生成的叙事正文
        """
        user_msg = build_user_prompt(user_input)
        full_user = f"{context}\n\n{user_msg}" if context else user_msg

        return self.llm.chat(
            system_prompt=system_prompt,
            user_prompt=full_user,
            temperature=0.85,
            max_tokens=4096,
        )
