"""Pinned AstrBot loads a new provider; discovery needs no central plugin list."""

import os
from pathlib import Path
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[2]


def test_new_plugin_loaded_by_real_astrbot_registers_automatically(tmp_path):
    source = textwrap.dedent(r'''
        import asyncio, copy, json, os
        from pathlib import Path
        from types import SimpleNamespace
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.star.star_manager import PluginManager
        from astrbot.core.star.context import Context
        from astrbot.core.star.star import star_registry
        import data.plugins
        from v2_console.workspace_registry import WorkspaceSettingsRegistry
        base=Path(os.environ['CASE_ROOT'])
        plugins=base/'plugins';probe=plugins/'v2_settings_probe';probe.mkdir(parents=True)
        (probe/'metadata.yaml').write_text('name: v2_settings_probe\nauthor: test\ndesc: Settings discovery test\nversion: 1.0.0\n')
        (probe/'main.py').write_text("from astrbot.api import star\n@star.register('v2_settings_probe', 'test', 'settings test', '1.0.0')\nclass Probe(star.Star):\n    async def initialize(self):\n        self.value={'enabled':False}\n    def workspace_settings(self):\n        def validate(value):\n            if set(value)!={'enabled'} or type(value['enabled']) is not bool: raise ValueError()\n            return dict(value)\n        async def read(identity): return dict(self.value)\n        async def apply(identity,before,after):\n            if self.value!=before: raise RuntimeError('conflict')\n            self.value=dict(after)\n        return [{'name':'config','validate':validate,'read':read,'apply':apply}]\n")
        data.plugins.__path__=[str(plugins),*data.plugins.__path__]
        config=copy.deepcopy(DEFAULT_CONFIG);config['plugin_set']=['*'];config['provider_settings']['enable']=False
        class CaseContext:
            get_all_stars=Context.get_all_stars
            get_registered_star=Context.get_registered_star
            conversation_manager=object()
            platform_manager=SimpleNamespace(get_insts=lambda:[])
            def get_config(self,**kwargs):return config
        class Volume:
            def verified_group_workspace(self,path):return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        async def main():
            context=CaseContext();manager=PluginManager(context,config);manager.plugin_store_path=str(plugins)
            loaded=await manager.load(specified_dir_name='v2_settings_probe');assert loaded[0],loaded
            parent=base/'group';parent.mkdir(mode=0o700);workspace=parent/'workspace';workspace.mkdir()
            router=SimpleNamespace(_initialized=True,_closing=False,allowed_conversations={'group:42'},
                _group_runtimes={'g':{'platform_id':'qq','bot_id':'1','group_id':'42','workspace':workspace,'volume_module':Volume()}})
            metadata=SimpleNamespace(name='v2_dsh_router',star_cls=router,activated=True)
            star_registry.append(metadata)
            registry=WorkspaceSettingsRegistry(context,base/'settings',SimpleNamespace(warning=lambda *a,**k:None))
            await registry.tick()
            path=workspace/'settings/v2_settings_probe.config.json'
            assert json.loads(path.read_text())['settings']=={'enabled':False},registry.errors
            plugin=context.get_registered_star('v2_settings_probe').star_cls
            doc=json.loads(path.read_text());doc['settings']['enabled']=True;path.write_text(json.dumps(doc))
            await registry.tick();assert plugin.value=={'enabled':True}
            await registry.detach(plugin)
            context.get_registered_star('v2_settings_probe').activated=False
            await registry.tick();assert json.loads((workspace/'settings/v2_settings_probe.config.status.json').read_text())['status']=='inactive'
            await registry.close()
            print('NATIVE_SETTINGS_PLUGIN_DISCOVERY_OK')
        asyncio.run(main())
    ''')
    runtime = ROOT / ".runtime/astrbot"
    result = subprocess.run(
        [str(runtime / ".venv/bin/python"), "-c", source],
        cwd=runtime,
        env={
            **os.environ,
            "CASE_ROOT": str(tmp_path),
            "ASTRBOT_ROOT": str(tmp_path / "astrbot"),
            "PYTHONPATH": str(ROOT / "astrbot-plugins"),
        },
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NATIVE_SETTINGS_PLUGIN_DISCOVERY_OK" in result.stdout
