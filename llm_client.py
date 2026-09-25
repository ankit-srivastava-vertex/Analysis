"""Unified Azure OpenAI LLM client for the Analysis project.

Provides a single, consistent interface for all LLM calls across modules.
Every call degrades gracefully: returns None when the API key is missing,
the endpoint is unreachable, or --no-llm is active. No module should ever
crash because the LLM is unavailable.

Config is read from .env in the project root:
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_VERSION   (default: 2024-12-01-preview)
    AZURE_OPENAI_DEPLOYMENT_NAME
"""

import json
import os
import re
import time
from typing import Optional

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

_cfg_cache: Optional[dict] = None


def load_config() -> dict:
    """Load Azure OpenAI config from .env (cached after first call)."""
    global _cfg_cache
    if _cfg_cache is not None:
        return _cfg_cache
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except ImportError:
        pass
    _cfg_cache = {
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
        "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip(),
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION",
                                      "2024-12-01-preview").strip(),
        "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "").strip(),
    }
    return _cfg_cache


def is_available() -> bool:
    """Check whether LLM config is present (key + endpoint + deployment)."""
    cfg = load_config()
    return bool(cfg["api_key"] and cfg["endpoint"] and cfg["deployment"])


def llm_call(system_prompt: str, user_prompt: str, *,
             json_mode: bool = False, max_tokens: int = 3000,
             timeout: int = 120) -> Optional[str]:
    """Call Azure OpenAI GPT-5.2 and return the response text, or None on any failure.

    Retries up to 3 times with exponential backoff on HTTP 429.
    """
    cfg = load_config()
    if not cfg["api_key"] or not cfg["endpoint"] or not cfg["deployment"]:
        return None

    url = (f"{cfg['endpoint'].rstrip('/')}/openai/deployments/"
           f"{cfg['deployment']}/chat/completions"
           f"?api-version={cfg['api_version']}")
    headers = {"Content-Type": "application/json", "api-key": cfg["api_key"]}
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=timeout)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            elif resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            else:
                print(f"    [llm_client] error {resp.status_code}: "
                      f"{resp.text[:200]}")
                return None
        except Exception as e:
            print(f"    [llm_client] exception: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def llm_json(system_prompt: str, user_prompt: str, *,
             max_tokens: int = 3000, timeout: int = 120) -> Optional[dict]:
    """Call Azure OpenAI with JSON mode and parse the response.

    Falls back to regex extraction of {...} or [...] from markdown fences
    when the raw response isn't valid JSON.
    """
    raw = llm_call(system_prompt, user_prompt, json_mode=True,
                   max_tokens=max_tokens, timeout=timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m2 = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', raw, re.DOTALL)
    if m2:
        try:
            return {"results": json.loads(m2.group(1))}
        except Exception:
            pass
    print(f"    [llm_client] invalid JSON response: {raw[:200]}")
    return None
