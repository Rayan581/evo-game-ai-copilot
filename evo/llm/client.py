"""LLM adapter: any OpenAI-compatible chat endpoint (Groq, Ollama, OpenRouter, ...).

Responses are cached in evo.db by a hash of (model, temperature, messages) so an
evaluation can be replayed offline with identical decisions. The API key is read
from the environment variable named in settings (default EVO_LLM_API_KEY) and is
never written to disk or logs.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from evo.memory.store import Store
from evo.util import jdumps, now_iso, sha256_text


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    text: str
    provider: str
    model: str
    prompt_hash: str
    latency_ms: int
    cached: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def _retry_after_s(resp, attempt: int) -> float:
    """Seconds to wait after an HTTP 429: Retry-After header, else 'try again in 1.5s', else backoff."""
    wait = None
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            wait = float(ra)
        except ValueError:
            wait = None
    if wait is None:
        m = re.search(r"try again in (?:(\d+)m)?\s*([\d.]+)s", resp.text or "")
        if m:
            wait = float(m.group(1) or 0) * 60 + float(m.group(2))
    if wait is None:
        wait = 5.0 * (attempt + 1)
    return min(max(wait, 1.0) + 1.0, 65.0)


def prompt_hash(model: str, temperature: float, messages: list[dict]) -> str:
    return sha256_text(jdumps({"model": model, "t": temperature, "m": messages}))[:24]


class OpenAICompatClient:
    provider = "openai_compat"

    def __init__(self, store: Store, cfg: dict[str, Any]):
        self.store = store
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.temperature = float(cfg.get("temperature", 0.0))
        self.timeout = int(cfg.get("timeout_s", 120))
        self.use_cache = bool(cfg.get("use_cache", True))
        self.api_key = os.environ.get(cfg.get("api_key_env", "EVO_LLM_API_KEY"), "")
        self.max_rate_retries = int(cfg.get("max_rate_retries", 6))
        self.calls = 0

    def chat(self, messages: list[dict]) -> LLMReply:
        ph = prompt_hash(self.model, self.temperature, messages)
        if self.use_cache:
            row = self.store.conn.execute("SELECT * FROM llm_cache WHERE prompt_hash=?", (ph,)).fetchone()
            if row:
                return LLMReply(row["response_text"], self.provider, self.model, ph, 0, True,
                                row["prompt_tokens"], row["completion_tokens"])
        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            raise LLMError("no API key: set EVO_LLM_API_KEY in .env (or use --provider offline)")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "response_format": {"type": "json_object"}}
        t0 = time.time()
        resp = None
        for rate_try in range(self.max_rate_retries + 1):
            try:
                resp = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=body,
                                     timeout=(10, self.timeout))
            except requests.RequestException as e:
                raise LLMError(f"request failed: {type(e).__name__}") from e
            self.calls += 1
            # Free tiers rate-limit by tokens per minute: wait as long as the server asks, then retry.
            if resp.status_code == 429 and rate_try < self.max_rate_retries:
                time.sleep(_retry_after_s(resp, rate_try))
                continue
            break
        latency = int((time.time() - t0) * 1000)
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        self.store.conn.execute(
            "INSERT OR REPLACE INTO llm_cache(prompt_hash, provider, model, response_text, latency_ms, prompt_tokens,"
            " completion_tokens, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (ph, self.provider, self.model, text, latency, usage.get("prompt_tokens"),
             usage.get("completion_tokens"), now_iso()),
        )
        return LLMReply(text, self.provider, self.model, ph, latency, False, usage.get("prompt_tokens"),
                        usage.get("completion_tokens"))


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a reply (tolerates ```json fences)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if not m:
            raise
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj
