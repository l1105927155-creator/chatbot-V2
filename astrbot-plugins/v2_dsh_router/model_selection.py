"""Interpret the pinned ACP model catalog without inferring model availability."""
import json


def model_route(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('模型选择必须包含provider和model。')
    if any(not isinstance(part, str) or not part or part != part.strip()
           or len(part) > 256 or any(ord(c) < 32 for c in part) for part in value):
        raise ValueError('模型标识无效。')
    return tuple(value)


def model_view(config_options):
    option = next((entry for entry in config_options if entry.get('id') == 'model'), None)
    if option is None:
        raise RuntimeError('DSH未公开模型目录。')
    try:
        current = model_route(json.loads(option['currentValue']))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('DSH当前模型状态无效。') from exc
    choices = []
    for group in option.get('options', []):
        for entry in group.get('options', []) if 'group' in group else [group]:
            try:
                provider, model = model_route(json.loads(entry['value']))
            except (KeyError, TypeError, ValueError):
                continue
            if not any(c['provider'] == provider and c['model'] == model for c in choices):
                choices.append({'provider': provider, 'model': model,
                                'name': entry.get('name') or entry.get('label') or model,
                                'value': entry['value']})
    return {'current': {'provider': current[0], 'model': current[1]}, 'choices': choices}
