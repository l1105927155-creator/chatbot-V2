"""Exercise the actual Router cleanup ordering without starting a Bot."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_memory_drains_before_clients_and_group_mounts():
    source = Path(__file__).resolve().parents[1] / "astrbot-plugins/v2_dsh_router/main.py"
    tree = ast.parse(source.read_text())
    router = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "V2DshRouter")
    method = next(node for node in router.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_close_runtime")
    namespace = {"asyncio": asyncio}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)  # noqa: S102 -- trusted repository method
    events = []

    async def memory_close():
        events.append("memory")

    async def background_close():
        assert events == ["memory"]
        events.append("background")

    def client_close():
        assert events == ["memory", "background"]
        events.append("client")

    def unmount(_workspace):
        assert events == ["memory", "background", "client"]
        events.append("unmount")

    obj = SimpleNamespace(memory_manager=SimpleNamespace(close=memory_close),
                          services={"group": SimpleNamespace(close_background=background_close)},
                          clients={"group": SimpleNamespace(close=client_close)},
                          _group_runtimes={"group": {"workspace": "test-volume",
                                                    "volume_module": SimpleNamespace(unmount_group=unmount)}})
    asyncio.run(namespace["_close_runtime"](obj))
    assert events == ["memory", "background", "client", "unmount"]
