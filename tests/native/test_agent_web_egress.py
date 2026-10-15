"""Production Agent Profile ACP must bypass ambient HTTP proxies for web fetch."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".runtime" / "astrbot" / ".venv" / "bin" / "python"


@pytest.mark.parametrize("domain", ["group", "owner"])
def test_production_acp_clears_ambient_proxy_and_blocks_private_dns(domain):
    worker = r'''
import http.server, json, os, shutil, sys, threading
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(os.environ["V2_REPO_ROOT"])
domain = os.environ["V2_EGRESS_DOMAIN"]
sys.path.insert(0, str(root / "astrbot-plugins" / "v2_dsh_router"))
from dsh_client import DshClient
import deepseek_harness.client as harness_client

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    hits = 0
    lock = threading.Lock()

    def _reply(self):
        with self.lock:
            type(self).hits += 1
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_CONNECT(self):
        self._reply()

    def do_GET(self):
        self._reply()

    def log_message(self, *_args):
        pass

proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
proxy_thread.start()
proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
proxy_names = (
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
)
previous_env = {name: os.environ.get(name) for name in (*proxy_names, "NODE_USE_ENV_PROXY", "NODE_OPTIONS")}
for name in proxy_names:
    os.environ[name] = proxy_url if name.lower() != "no_proxy" else ""
os.environ["NODE_USE_ENV_PROXY"] = "1"
os.environ["NODE_OPTIONS"] = "--experimental-strip-types"

with TemporaryDirectory(prefix="agent-web-egress-", dir=root / ".runtime") as tmp:
    base = Path(tmp)
    home, workspace = base / "dsh-home", base / "workspace"
    home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    result = base / "egress-result.json"
    observer_patch = base / "observer.patch.yml"
    observer_patch.write_text(
        "- insert:\n"
        "    - id: agent-profile-web-egress-observer\n"
        "      name: " + str(root / "tests/native/agent-profile-web-egress-probe.mjs") + "\n",
        encoding="utf-8",
    )
    resolver_source = base / "pinned-resolver" / "network.ts"
    resolver_source.parent.mkdir(mode=0o700)
    shutil.copyfile(
        root / ".runtime/dsh-source/packages/web/web-fetch-http/src/network.ts",
        resolver_source,
    )
    (resolver_source.parent / "node_modules").symlink_to(root / "dsh/node_modules", target_is_directory=True)
    os.environ["V2_WEB_EGRESS_RESULT"] = str(result)
    os.environ["V2_WEB_EGRESS_RESOLVER_MODULE"] = str(resolver_source)

    real_harness_client = harness_client.HarnessClient
    captured = {}
    class harness_client_with_observer(real_harness_client):
        def __init__(self, config=None, *args, **kwargs):
            if config is not None:
                captured["profile"] = config.profile
                captured["env"] = dict(config.env or {})
                config.patches = (*config.patches, str(observer_patch))
            super().__init__(config, *args, **kwargs)
    harness_client.HarnessClient = harness_client_with_observer

    client = None
    try:
        client = DshClient(
            dsh_bin=root / ".runtime/dsh-runtime/node_modules/.bin/dsh",
            dsh_home=home,
            workspace=workspace,
            patch=root / f"dsh/profile/v2-agent-{domain}.patch.yml",
            instructions=root / f"dsh/profile/agent-{domain}.md",
            provider="agent-profile-web-egress-no-inference",
            model="scripted-no-inference",
            enable_authorization=(domain == "owner"),
        )
        assert captured["profile"] == "sdk-minimal", captured
        assert all(captured["env"][name] == "" for name in proxy_names), captured
        assert captured["env"]["NODE_USE_ENV_PROXY"] == "0", captured
        session_id = client.create_session()
        # The observer runs in this real ACP child while the production
        # HttpFetchProvider attempts its controlled private-host lookup.
        import time
        deadline = time.monotonic() + 5
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert result.exists(), "ACP observer did not run for agent/created"
        observed = json.loads(result.read_text(encoding="utf-8"))
        assert "probeFailure" not in observed, observed
        observed["runtime_profile"] = captured["profile"]
        client.close_session(session_id)
    except BaseException as error:
        print(f"worker failure: {type(error).__name__}: {error!r} data={getattr(error, 'data', None)!r}", file=sys.stderr)
        if client is not None:
            print("\n".join(client.runtime.client._stderr_lines), file=sys.stderr)
        raise
    finally:
        if client is not None:
            client.close()
        harness_client.HarnessClient = real_harness_client
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=1)
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    assert observed["runtime_profile"] == "sdk-minimal", observed
    assert observed["routeProxied"] is False, observed
    assert observed["resolverCalls"] > 0, observed
    assert observed["fetchError"]["code"] == "WEB_BLOCKED_URL", observed
    assert "non-public IP address" in observed["fetchError"]["message"], observed
    assert all(observed["environment"][name] == "" for name in proxy_names), observed
    assert observed["environment"]["NODE_USE_ENV_PROXY"] == "0", observed
    assert ProxyHandler.hits == 0, f"the local proxy received {ProxyHandler.hits} request(s)"
    print(json.dumps({
        "production_dsh_client_acp": True,
        "domain": domain,
        "runtime_profile": observed["runtime_profile"],
        "proxy_route": observed["routeProxied"],
        "private_dns_resolver_calls": observed["resolverCalls"],
        "private_target_rejected": observed["fetchError"]["code"],
        "localhost_proxy_connections": ProxyHandler.hits,
        "public_network_requests": 0,
    }))
'''
    env = {**os.environ, "V2_REPO_ROOT": str(ROOT), "V2_EGRESS_DOMAIN": domain}
    process = subprocess.Popen(
        [str(PYTHON), "-c", worker], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise AssertionError(f"production Agent Profile egress test exceeded 90 seconds\n{stdout}\n{stderr}")
    assert process.returncode == 0, stdout + stderr
    assert '"production_dsh_client_acp": true' in stdout
    assert '"private_target_rejected": "WEB_BLOCKED_URL"' in stdout
