"""이미지 없는 JSON 요청 (용어 후보 뽑기·번역 등). 추출과 같은 두 엔진을 쓴다.

    data, api_usd, equiv_usd = complete_json(client, model_id, system, prompt, schema, work_dir)

- client가 ClaudeCodeClient면 `claude -p`(요금제), 아니면 Anthropic API client.
- 한도·과부하·네트워크 -> app.extract.Paused (다시 부르면 이어 간다)
- 이 요청만의 문제 -> app.extract_cc.CardError
- 로그인·키 문제 -> app.extract_cc.NotAvailable
"""

from __future__ import annotations

import json
from pathlib import Path

from app.extract import FALLBACK_BETA, SERVER_FALLBACK_MODELS, classify_error, cost_usd


def complete_json(client, model: str, system: str, prompt: str, schema: dict,
                  work_dir: Path) -> tuple[dict, float, float]:
    from app.extract_cc import CardError

    if getattr(client, "engine", "api") == "claude-code":
        reply = client.ask_json(prompt, schema, model, system, work_dir)
        if not isinstance(reply.regions_json, dict):
            raise CardError("구조화 출력이 없음")
        return reply.regions_json, 0.0, reply.cost_equiv_usd
    params = {
        "model": model,
        "max_tokens": 32000,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    try:
        if model in SERVER_FALLBACK_MODELS:
            msg = client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            msg = client.messages.create(**params)
    except Exception as e:  # noqa: BLE001
        p = classify_error(e)
        if p is None:
            raise CardError(f"요청 오류: {getattr(e, 'message', e)}")
        raise p
    if getattr(msg, "stop_reason", None) in ("refusal", "max_tokens"):
        raise CardError(f"응답이 끝나지 않음: {msg.stop_reason}")
    try:
        text = next(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(text)
    except (StopIteration, json.JSONDecodeError) as e:
        raise CardError(f"응답 오류: {e}")
    return data, cost_usd(msg.usage, model) if getattr(msg, "usage", None) else 0.0, 0.0
