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

from app.extract import SERVER_FALLBACK_MODELS, classify_error, cost_usd, send_message


def _content(prompt: str, images: list[Path] | None):
    if not images:
        return prompt
    import base64

    blocks = []
    for p in images:
        blocks.append({"type": "text", "text": f"Image file: {Path(p).name}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                   "data": base64.standard_b64encode(Path(p).read_bytes()).decode()}})
    return blocks + [{"type": "text", "text": prompt}]


def complete_json(client, model: str, system: str, prompt: str, schema: dict,
                  work_dir: Path, images: list[Path] | None = None) -> tuple[dict, float, float]:
    """images: 함께 볼 PNG (예: 아이콘 이름 제안). 지시문에는 파일 이름으로 가리킨다."""
    from app.extract_cc import CardError

    if getattr(client, "engine", "api") == "claude-code":
        reply = client.ask_json(prompt, schema, model, system, work_dir, images=images)
        if not isinstance(reply.regions_json, dict):
            raise CardError("구조화 출력이 없음")
        return reply.regions_json, 0.0, reply.cost_equiv_usd
    params = {
        "model": model,
        "max_tokens": 32000,
        "system": system,
        "messages": [{"role": "user", "content": _content(prompt, images)}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    try:
        msg = send_message(client, params, model in SERVER_FALLBACK_MODELS)  # 스트리밍 (긴 응답)
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
