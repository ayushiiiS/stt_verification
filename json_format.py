"""Pretty-print transcript JSON with numbering and clear separation."""

from __future__ import annotations

import json
from pathlib import Path


def number_messages(messages: list[dict]) -> list[dict]:
    numbered: list[dict] = []
    for i, msg in enumerate(messages, start=1):
        entry = {k: v for k, v in msg.items() if k != "n"}
        numbered.append({"n": i, **entry})
    return numbered


def number_transcript_store(
    data: dict[str, dict],
    call_order: list[str] | None = None,
) -> dict[str, dict]:
    """Return a copy with call `number` and per-message `n` fields."""
    if call_order is None:
        call_order = sorted(data.keys())

    order_index = {cid: i for i, cid in enumerate(call_order, start=1)}
    extras = [cid for cid in data if cid not in order_index]
    for i, cid in enumerate(extras, start=len(order_index) + 1):
        order_index[cid] = i

    result: dict[str, dict] = {}
    for call_id, entry in data.items():
        numbered_entry = {k: v for k, v in entry.items() if k != "number"}
        if "messages" in numbered_entry and isinstance(numbered_entry["messages"], list):
            numbered_entry["messages"] = number_messages(numbered_entry["messages"])
        ordered: dict = {"number": order_index.get(call_id, 0)}
        ordered.update(numbered_entry)
        result[call_id] = ordered
    return result


def _dump_value(value, indent: int) -> str:
    pad = " " * indent
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if "\n" not in rendered:
        return rendered
    lines = rendered.splitlines()
    return lines[0] + "\n" + "\n".join(pad + line for line in lines[1:])


def format_call_entry(call_id: str, entry: dict, indent: int = 2) -> str:
    """Format one call entry with blank lines between messages."""
    pad = " " * indent
    inner = pad + "  "
    lines: list[str] = [f'{pad}"{call_id}": {{']

    items = list(entry.items())
    for idx, (key, value) in enumerate(items):
        comma = "," if idx < len(items) - 1 else ""
        if key == "messages" and isinstance(value, list):
            lines.append(f'{inner}"messages": [')
            for m_idx, msg in enumerate(value):
                msg_json = json.dumps(msg, ensure_ascii=False, indent=2)
                msg_pad = inner + "  "
                msg_lines = msg_json.splitlines()
                block = msg_pad + msg_lines[0]
                if len(msg_lines) > 1:
                    block += "\n" + "\n".join(msg_pad + line for line in msg_lines[1:])
                if m_idx < len(value) - 1:
                    lines.append(block + ",")
                    lines.append("")  # blank line between messages
                else:
                    lines.append(block)
            lines.append(f"{inner}]{comma}")
        else:
            lines.append(f'{inner}"{key}": {_dump_value(value, indent + 4)}{comma}')

    lines.append(f"{pad}}}")
    return "\n".join(lines)


def dumps_numbered(
    data: dict[str, dict],
    call_order: list[str] | None = None,
) -> str:
    numbered = number_transcript_store(data, call_order)

    if call_order is None:
        keys = sorted(numbered.keys())
    else:
        seen = set(call_order)
        keys = [cid for cid in call_order if cid in numbered]
        keys.extend(sorted(cid for cid in numbered if cid not in seen))

    if not keys:
        return "{}\n"

    blocks = [format_call_entry(call_id, numbered[call_id]) for call_id in keys]
    return "{\n" + ",\n\n".join(blocks) + "\n}\n"


def dump_numbered(
    path: Path,
    data: dict[str, dict],
    call_order: list[str] | None = None,
) -> None:
    text = dumps_numbered(data, call_order)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    tmp.replace(path)
