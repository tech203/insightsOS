def normalize_result(action: str, raw_result):
    if isinstance(raw_result, dict):
        data = raw_result
    else:
        data = {"raw": raw_result}

    return {
        "type": action,
        "summary": data.get("summary", ""),
        "data": data.get("data", data),
        "insights": data.get("insights", []),
        "next_actions": data.get("next_actions", []),
        "meta": data.get("meta", {}),
    }