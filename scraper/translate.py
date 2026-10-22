"""可选：调用 AI 治理里配置的 OpenAI 兼容接口，把标题 / 简介翻译成简体中文（原文保留在 originaltitle）"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

import requests

from .models import Metadata

TRANSLATE_OFF = "off"
TRANSLATE_TITLE = "title"
TRANSLATE_TITLE_PLOT = "title_plot"
TRANSLATE_MODES = (TRANSLATE_OFF, TRANSLATE_TITLE, TRANSLATE_TITLE_PLOT)

_KANA = re.compile(r'[぀-ヿ]')
_LATIN = re.compile(r'[A-Za-z]{3,}')


def _needs(text: str) -> bool:
    """含假名或以英文为主的文本才翻译；已是中文的跳过"""
    if not text:
        return False
    if _KANA.search(text):
        return True
    cjk = len(re.findall(r'[一-鿿]', text))
    return cjk == 0 and bool(_LATIN.search(text))


def translate(meta: Metadata, mode: str, ai_cfg: Dict[str, Any]) -> bool:
    """原地修改 meta；失败时保持原文，返回是否翻译成功"""
    if mode not in (TRANSLATE_TITLE, TRANSLATE_TITLE_PLOT) or not ai_cfg.get("api_key"):
        return False
    fields: Dict[str, str] = {}
    if _needs(meta.title):
        fields["title"] = meta.title
    if mode == TRANSLATE_TITLE_PLOT and _needs(meta.plot):
        fields["plot"] = meta.plot[:2000]
    if not fields:
        return False
    endpoint = f"{str(ai_cfg.get('api_base') or 'https://openrouter.ai/api/v1').rstrip('/')}/chat/completions"
    prompt = (
        "把下面 JSON 中每个字段翻译成自然流畅的简体中文，人名保持原文，番号保持原样。"
        "只输出同结构的 JSON，不要任何解释。\n" + json.dumps(fields, ensure_ascii=False)
    )
    try:
        r = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {ai_cfg['api_key']}", "Content-Type": "application/json"},
            json={"model": ai_cfg.get("model") or "", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=45,
        )
        if r.status_code != 200:
            return False
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r'\{.*\}', content, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return False
    changed = False
    title = str(data.get("title") or "").strip()
    if "title" in fields and title:
        meta.original_title = meta.original_title or meta.title
        meta.title = title
        changed = True
    plot = str(data.get("plot") or "").strip()
    if "plot" in fields and plot:
        meta.plot = plot
        changed = True
    return changed
