"""Small QQ command presentation for the actual per-conversation DSH model."""
import shlex


def select_route(arguments, choices):
    parts = shlex.split(arguments)
    if parts == ['default']:
        return None
    if len(parts) == 2:
        matches = [c for c in choices if (c['provider'], c['model']) == tuple(parts)]
    elif len(parts) == 1:
        matches = [c for c in choices if c['model'] == parts[0]]
    else:
        raise ValueError('用法：/provider <provider> <model>，或 /provider default。')
    if len(matches) != 1:
        raise ValueError('模型不存在或名称不唯一，请用 /provider list 查看完整标识。')
    return matches[0]['provider'], matches[0]['model']


def describe_model(view, *, listing=False):
    current = view['current']
    lines = [f"当前模型：{current['provider']} / {current['model']}",
             '本会话独立保存模型设置。' if view.get('snapshot') else
             ('本会话已单独选择模型。' if view.get('overridden') else '本会话跟随部署默认模型。')]
    if view.get('current_available') is False:
        lines.append('当前模型已不在共享目录中；会话选择保留，请按需重新选择模型。')
    if listing:
        lines.append('可用模型：')
        lines.extend(f"{c['provider']} / {c['model']}" for c in view['choices'])
    lines.append('查看目录：/provider list；切换：/provider <provider> <model>；恢复默认：/provider default。')
    return '\n'.join(lines)
