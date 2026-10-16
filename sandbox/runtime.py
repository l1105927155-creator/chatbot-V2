"""Isolated foreground runtime for the QQ message sandbox.

This stages a private AstrBot root and V2 plugin snapshot, then connects it to
the sandbox OneBot server. It never reads the production AstrBot config/data.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / ".runtime/qq-sandbox"
MANIFEST = "sandbox-runtime.json"
SANDBOX_PLATFORM_ID = "v2-sandbox"
SANDBOX_BOT_ID = "99000"


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # TIME_WAIT from our last closed listener must not block a restart.
        # A live listener still prevents this bind (SO_REUSEPORT is not used).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def _assert_ports_free(ports: dict[str, int]) -> None:
    seen: set[int] = set()
    for name, port in ports.items():
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid {name} port: {port}")
        if port in seen:
            raise ValueError(f"Sandbox ports must be distinct; {name} repeats {port}")
        seen.add(port)
        if not _port_free("127.0.0.1", port):
            raise RuntimeError(
                f"Sandbox port {port} ({name}) is already occupied; refusing to start"
            )


def _pinned_astrbot(source: Path) -> None:
    lock = json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))
    expected = lock["astrbot"]["ref"]
    expected_tag = lock["astrbot"]["tag_object_sha"]
    if not (source / ".git").exists():
        raise RuntimeError(f"AstrBot source is not a Git checkout: {source}")
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    # A bootstrapped checkout records the verified annotated tag in this ref.
    try:
        actual_tag = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", f"refs/v2-bootstrap/tags/{expected}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        ref_commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", f"refs/v2-bootstrap/tags/{expected}^{{commit}}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        actual_tag, ref_commit = "", ""
    if actual_tag != expected_tag or not ref_commit or head != ref_commit:
        raise RuntimeError(
            f"AstrBot checkout must be verified at pinned {expected}; "
            f"HEAD={head}, pinned commit={ref_commit or 'unavailable'}, tag={actual_tag or 'unverified'}"
        )


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Expected a real directory: {path}")
    path.chmod(0o700)


def _token(path: Path) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Unsafe OneBot token path: {path}")
        if path.stat().st_mode & 0o077:
            raise RuntimeError(f"OneBot token file must be private (0600): {path}")
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32:
            raise RuntimeError(f"Invalid OneBot token file: {path}")
        return value
    value = secrets.token_urlsafe(40)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(value + "\n")
    return value


def _child_environment(*, dsh_enabled: bool, home: Path, scripted: bool = False) -> dict[str, str]:
    """Keep normal runtime settings while dropping unrelated credentials/live paths."""
    inherited = {"PATH", "LANG", "TERM", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
                 "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
    inherited.update(key for key in os.environ if key.startswith("LC_"))
    clean = {key: os.environ[key] for key in inherited if os.environ.get(key)}
    clean.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                  "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
                  "TMPDIR": str(home / "tmp")})
    # DSH's native managed scopes use the current user's systemd bus for
    # resource limits/cancellation. This control socket is never bound into
    # the Owner Bash sandbox, whose environment and mounts remain restricted.
    runtime_bus = Path("/run/user") / str(os.getuid())
    if runtime_bus.is_dir() and runtime_bus.stat().st_uid == os.getuid():
        clean["XDG_RUNTIME_DIR"] = str(runtime_bus)
        clean["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(runtime_bus / "bus")
    if dsh_enabled:
        for key in ("V2_DSH_PROVIDER", "V2_DSH_MODEL"):
            value = os.environ.get(key)
            if value:
                clean[key] = value
        key_value = os.environ.get("DEEPSEEK_API_KEY")
        if key_value and not scripted:
            clean["DEEPSEEK_API_KEY"] = key_value
    return clean


def _owner_identity(owner_id: str | int | None) -> dict[str, str] | None:
    if owner_id is None:
        return None
    value = str(owner_id)
    if (not re.fullmatch(r"[1-9][0-9]{0,19}", value)
            or value in {SANDBOX_BOT_ID, "90001", "90002", "91000"}):
        raise ValueError("--owner-id must be a canonical positive synthetic QQ id distinct from sandbox actors")
    return {"platform_id": SANDBOX_PLATFORM_ID, "bot_id": SANDBOX_BOT_ID,
            "user_id": value}


def _assert_owner_unchanged(previous: dict, owner: dict[str, str] | None) -> None:
    if previous.get("owner") != owner:
        raise RuntimeError("Sandbox owner identity is immutable; use a fresh data directory to change it")


def _stage_dsh_repo(staged_repo: Path, data_dir: Path, dsh_mode: str) -> None:
    """Stage production profiles as real files and add the sandbox-only probe."""
    source = ROOT / "dsh"
    target = staged_repo / "dsh"
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.mkdir(mode=0o700)
    profiles = target / "profile"
    shutil.copytree(source / "profile", profiles,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("plugins", "skills"):
        link = target / name
        if (source / name).exists():
            link.symlink_to(source / name, target_is_directory=True)
    runtime_modules = ROOT / ".runtime/dsh-runtime/node_modules"
    if runtime_modules.is_dir():
        (target / "node_modules").symlink_to(runtime_modules, target_is_directory=True)
    if dsh_mode in {"live", "scripted"}:
        probe_source = ROOT / "sandbox/probe.mjs"
        if not probe_source.is_file():
            raise RuntimeError("sandbox DSH probe is missing: sandbox/probe.mjs")
        probe_dir = staged_repo / "sandbox"
        _mkdir_private(probe_dir)
        staged_probe = probe_dir / "probe.mjs"
        shutil.copy2(probe_source, staged_probe)
        staged_probe.chmod(0o600)
        for profile_name in ("v2-agent-group.patch.yml", "v2-agent-owner.patch.yml",
                             "v2-agent-group-sandbox.patch.yml"):
            if not (profiles / profile_name).exists():
                continue
            patch = profiles / profile_name
            with patch.open("a", encoding="utf-8") as stream:
                stream.write(
                    "\n- insert:\n"
                    "    - id: v2-sandbox-probe\n"
                    f"      name: {json.dumps(str(staged_probe))}\n"
                    "      config:\n"
                    "        controlUrl: !!js process.env.SANDBOX_CONTROL_URL\n"
                    "        token: !!js process.env.SANDBOX_CONTROL_TOKEN\n"
                    "        mode: !!js process.env.SANDBOX_DSH_MODE\n"
                )
    runtime_dir = staged_repo / ".runtime"
    _mkdir_private(runtime_dir)
    for name in ("dsh-runtime", "dsh-home-chat", "dsh-workspace-chat"):
        path = runtime_dir / name
        if name == "dsh-runtime":
            upstream = ROOT / ".runtime/dsh-runtime"
            if path.is_symlink():
                if path.resolve() != upstream.resolve():
                    raise RuntimeError(f"Staged DSH runtime link is unexpected: {path}")
            elif path.exists():
                raise RuntimeError(f"Staged DSH runtime path is not a symlink: {path}")
            elif upstream.exists():
                path.symlink_to(upstream, target_is_directory=True)
        else:
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise RuntimeError(f"Staged DSH state path is unsafe: {path}")
            _mkdir_private(path)
    if dsh_mode in {"live", "scripted"}:
        for name in ("dsh-home-owner", "dsh-workspace-owner"):
            path = runtime_dir / name
            if path.is_symlink():
                raise RuntimeError(f"Owner runtime path must not be a symlink: {path}")
            if path.exists() and not path.is_dir():
                raise RuntimeError(f"Owner runtime path must be a directory: {path}")
            _mkdir_private(path)


def prepare(data_dir: Path, astrbot_source: Path, ports: dict[str, int], dsh_mode: str,
            owner_id: str | int | None = None) -> dict:
    data_dir = Path(os.path.abspath(data_dir.expanduser()))
    if dsh_mode not in {"live", "scripted", "disabled"}:
        raise ValueError("invalid sandbox DSH mode")
    owner = _owner_identity(owner_id)
    if owner is not None and dsh_mode == "disabled":
        raise ValueError("--owner-id requires an enabled DSH runtime")
    if data_dir.is_symlink() or data_dir.resolve(strict=False) != data_dir:
        raise RuntimeError(f"Sandbox data directory must not be a symlink: {data_dir}")
    source = astrbot_source.expanduser().resolve(strict=True)
    _pinned_astrbot(source)
    if dsh_mode in {"live", "scripted"} and not (ROOT / "sandbox/probe.mjs").is_file():
        raise RuntimeError("sandbox DSH probe is missing: sandbox/probe.mjs")
    protected = (source, ROOT / ".runtime/astrbot", ROOT / ".runtime/dsh-runtime",
                 ROOT / ".runtime/dsh-source", ROOT / ".runtime/dsh-source-full.zip",
                 ROOT / ".runtime/dsh-home-chat", ROOT / ".runtime/dsh-home-owner",
                 ROOT / ".runtime/dsh-workspace-chat", ROOT / ".runtime/dsh-workspace-owner",
                 ROOT / "astrbot-plugins", ROOT / "dsh")
    for protected_path in protected:
        protected_path = protected_path.resolve(strict=False)
        if data_dir == protected_path or data_dir in protected_path.parents or protected_path in data_dir.parents:
            raise RuntimeError(f"Sandbox data directory overlaps protected runtime/code path: {protected_path}")
    python = source / ".venv/bin/python"
    dsh_bin = ROOT / ".runtime/dsh-runtime/node_modules/.bin/dsh"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"Pinned AstrBot virtualenv is missing: {python}")
    dsh_runtime = ROOT / ".runtime/dsh-runtime"
    if dsh_mode in {"live", "scripted"}:
        package = dsh_runtime / "node_modules/@deepseek-ai/dsh/package.json"
        dsh_source = ROOT / ".runtime/dsh-source"
        dsh_archive = ROOT / ".runtime/dsh-source-full.zip"
        if not dsh_bin.is_file() or not package.is_file():
            raise RuntimeError(f"Pinned DSH runtime is missing: {dsh_runtime}")
        package_info = json.loads(package.read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))
        if package_info.get("version") != lock["deepseek_harness"]["version"]:
            raise RuntimeError("Installed DSH package version does not match upstream.lock.json")
        marker = dsh_source / ".v2-pinned-commit"
        if not marker.is_file() or marker.read_text(encoding="ascii").strip() != lock["deepseek_harness"]["ref"]:
            raise RuntimeError("DSH source checkout does not match upstream.lock.json")
        subprocess.run([sys.executable, str(ROOT / "scripts/verify-dsh-source.py"),
                        str(ROOT / "upstream.lock.json"), str(dsh_archive), str(dsh_source)], check=True)

    manifest_path = data_dir / MANIFEST
    if manifest_path.is_symlink():
        raise RuntimeError(f"Sandbox manifest must not be a symlink: {manifest_path}")
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("format") != 1 or Path(previous.get("data_dir", "")).resolve() != data_dir.resolve():
            raise RuntimeError(f"Refusing to adopt unrelated sandbox directory: {data_dir}")
        _assert_owner_unchanged(previous, owner)
        if dsh_mode != "disabled" and previous.get("execution_contract") != 2:
            raise RuntimeError("Sandbox execution contract changed; use a fresh --data-dir; existing sessions are preserved")
    elif data_dir.exists() and any(data_dir.iterdir()):
        # Existing unmanifested directories are never implicitly adopted.
        raise RuntimeError(f"Refusing to adopt unmanifested sandbox directory: {data_dir}")
    _mkdir_private(data_dir)

    astrbot_root = data_dir / "astrbot"
    _mkdir_private(astrbot_root)
    astrbot_data = astrbot_root / "data"
    if astrbot_data.is_symlink() or (astrbot_data.exists() and not astrbot_data.resolve().is_relative_to(astrbot_root.resolve())):
        raise RuntimeError(f"Sandbox AstrBot data path escapes its root: {astrbot_data}")
    config_path = astrbot_root / "data/cmd_config.json"
    if config_path.is_symlink():
        raise RuntimeError(f"Sandbox config must not be a symlink: {config_path}")
    config_preexisted = config_path.exists()
    token_path = data_dir / "onebot/access-token"
    _mkdir_private(token_path.parent)
    token = _token(token_path)
    # Load the pinned upstream defaults and write only to this sandbox root.
    sandbox_home = data_dir / "home"
    _mkdir_private(sandbox_home)
    for child_dir in (".config", ".cache", ".local/share", "tmp"):
        _mkdir_private(sandbox_home / child_dir)
    env = _child_environment(dsh_enabled=False, home=sandbox_home)
    env["ONEBOT_ACCESS_TOKEN"] = token
    env["SANDBOX_DSH_MODE"] = dsh_mode
    env["SANDBOX_ONEBOT_PORT"] = str(ports["onebot"])
    env["ASTRBOT_ROOT"] = str(astrbot_root)
    env["ASTRBOT_SOURCE"] = str(source)
    env["SANDBOX_CONFIG_PREEXISTED"] = "1" if config_preexisted else "0"
    code = r'''import copy,json,os
from pathlib import Path
from astrbot.core.config.default import DEFAULT_CONFIG
c=copy.deepcopy(DEFAULT_CONFIG)
c["dashboard"]["enable"]=False
c["provider_settings"]["enable"]=False
c["platform_settings"]["empty_mention_waiting"]=False
c["disable_metrics"]=True
c["admins_id"]=[]
c["platform"]= [{"id":"v2-sandbox","type":"aiocqhttp","enable":True,
 "ws_reverse_host":"127.0.0.1","ws_reverse_port":int(os.environ["SANDBOX_ONEBOT_PORT"]),
 "ws_reverse_token":os.environ["ONEBOT_ACCESS_TOKEN"]}]
p=Path(os.environ["ASTRBOT_ROOT"])/"data/cmd_config.json"
p.parent.mkdir(parents=True,exist_ok=True)
loaded=json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
for key in ("password","pbkdf2_password","password_storage_upgraded","password_change_required","jwt_secret"):
 if key in loaded.get("dashboard",{}):
  c["dashboard"][key]=loaded["dashboard"][key]
if os.environ["SANDBOX_CONFIG_PREEXISTED"] == "1":
 comparable=copy.deepcopy(loaded)
 comparable["platform"]=c["platform"]
 # This explicit V2 behavior update hands bare mentions to DSH. Preserve
 # existing sandbox data and reject unrelated configuration differences.
 if comparable.get("platform_settings",{}).get("empty_mention_waiting") is True:
  comparable["platform_settings"]["empty_mention_waiting"]=False
 if comparable != c:
  raise SystemExit("Existing sandbox config does not match isolated defaults")
p.write_text(json.dumps(c,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
p.chmod(0o600)
'''
    # Resolve defaults using the pinned Python and source tree, never production config.
    existing_config = config_path.read_bytes() if config_preexisted else None
    generated = subprocess.run([str(python), "-c", code], cwd=source, env=env,
                               check=False, capture_output=True, text=True)
    if generated.returncode:
        if existing_config is not None:
            config_path.write_bytes(existing_config)
        detail = generated.stderr.strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Pinned AstrBot rejected sandbox config")

    staged_repo = data_dir / "repo"
    _mkdir_private(staged_repo)
    plugins_root = staged_repo / "astrbot-plugins"
    if plugins_root.is_symlink():
        raise RuntimeError(f"Staged plugin root must not be a symlink: {plugins_root}")
    plugin_names = ["v2_ai_gate", "v2_journal", "v2_dsh_router", "v2_phase1_probe", "v2_sandbox_fixture"]
    fixture = ROOT / "sandbox/fixture_plugin"
    if not fixture.is_dir():
        plugin_names.remove("v2_sandbox_fixture")
    for name in plugin_names:
        target = staged_repo / "astrbot-plugins" / name
        original = fixture if name == "v2_sandbox_fixture" else ROOT / "astrbot-plugins" / name
        if target.is_symlink():
            raise RuntimeError(f"Staged plugin snapshot must not be a symlink: {target}")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        plugin_target = astrbot_root / "data/plugins" / name
        if name == "v2_dsh_router" and dsh_mode == "disabled":
            if plugin_target.is_symlink():
                plugin_target.unlink()
            continue
        if name == "v2_sandbox_fixture":
            plugin_target = astrbot_root / "data/plugins/v2_sandbox_fixture"
        plugin_root = astrbot_root / "data/plugins"
        for checked in (astrbot_root / "data", plugin_root):
            if checked.is_symlink():
                raise RuntimeError(f"Sandbox AstrBot path must not be a symlink: {checked}")
        plugin_target.parent.mkdir(parents=True, exist_ok=True)
        if plugin_target.is_symlink():
            if plugin_target.resolve() != target.resolve():
                raise RuntimeError(f"Sandbox plugin link points outside its staged snapshot: {plugin_target}")
        elif plugin_target.exists():
            raise RuntimeError(f"Refusing to replace non-symlink plugin path: {plugin_target}")
        else:
            plugin_target.symlink_to(target, target_is_directory=True)
    for name in ("dsh",):
        target = staged_repo / name
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.symlink_to(ROOT / name, target_is_directory=True)
    runtime_dir = staged_repo / ".runtime"
    if runtime_dir.is_symlink():
        raise RuntimeError(f"Staged runtime directory must not be a symlink: {runtime_dir}")
    _mkdir_private(runtime_dir)
    if dsh_runtime.exists():
        if (runtime_dir / "dsh-runtime").is_symlink():
            (runtime_dir / "dsh-runtime").unlink()
        elif (runtime_dir / "dsh-runtime").exists():
            shutil.rmtree(runtime_dir / "dsh-runtime")
        (runtime_dir / "dsh-runtime").symlink_to(dsh_runtime, target_is_directory=True)
    elif dsh_mode in {"live", "scripted"}:
        raise RuntimeError(f"Pinned DSH runtime is missing: {dsh_runtime}")

    _stage_dsh_repo(staged_repo, data_dir, dsh_mode)

    onebot_dir = data_dir / "onebot"
    _mkdir_private(onebot_dir)
    details = {
        "format": 1, "execution_contract": 2, "data_dir": str(data_dir), "astrbot_source": str(source),
        "astrbot_root": str(astrbot_root), "staged_repo": str(staged_repo),
        "plugin_snapshot": str(staged_repo / "astrbot-plugins"),
        "onebot_dir": str(onebot_dir), "onebot_token_file": str(token_path),
        "scenario_db": str(onebot_dir / "sandbox.sqlite3"),
        "home": str(sandbox_home),
        "ports": ports, "dsh_mode": dsh_mode,
        "members": [90001, 90002], "group": 91000, "bot": 99000,
        "platform_id": SANDBOX_PLATFORM_ID,
        "owner": owner,
    }
    temp = manifest_path.with_suffix(".tmp")
    temp.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(manifest_path)
    return details


def _owner_volume(details: dict, action: str, size_mib: int = 1024) -> None:
    """Use the production bounded ext4 implementation for this sandbox only."""
    repo = Path(details["staged_repo"])
    workspace = repo / ".runtime/dsh-workspace-owner"
    image = Path(details["data_dir"]) / "owner-volume/workspace.ext4"
    source = ROOT / "dsh/plugins/agent-profile/owner_volume.py"
    import importlib.util
    spec = importlib.util.spec_from_file_location("v2_sandbox_owner_volume", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("production owner volume implementation is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if action == "prepare":
        module.prepare(workspace, image, size_mib * 1024 ** 2)
    else:
        module.unmount(workspace)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated QQ message sandbox")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--astrbot-dir", type=Path, default=ROOT / ".runtime/astrbot")
    parser.add_argument("--onebot-port", type=int, default=6220)
    parser.add_argument("--ui-port", type=int, default=6221)
    parser.add_argument("--mcp-port", type=int, default=6222)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--owner-id", help="Opt into one immutable synthetic Owner private chat")
    parser.add_argument("--owner-volume-mib", type=int, default=1024,
                        help="Deprecated compatibility option; Owner now uses host execution, groups always have 1024 MiB")
    parser.add_argument("--no-dsh", action="store_true", help="Disable free-form DSH replies")
    parser.add_argument("--dsh-mode", choices=("live", "scripted", "disabled"), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.no_dsh and args.dsh_mode in {"live", "scripted"}:
        parser.error("--no-dsh conflicts with an enabled --dsh-mode")
    mode = args.dsh_mode or ("disabled" if args.no_dsh else "live")
    try:
        owner = _owner_identity(args.owner_id)
    except ValueError as exc:
        parser.error(str(exc))
    if owner and mode == "disabled":
        parser.error("--owner-id requires an enabled DSH runtime")
    if not 32 <= args.owner_volume_mib <= 1024 * 1024:
        parser.error("--owner-volume-mib must be between 32 and 1048576")
    if mode == "scripted":
        os.environ.setdefault("V2_DSH_PROVIDER", "sandbox-scripted")
        os.environ.setdefault("V2_DSH_MODEL", "scripted")
    ports = {"onebot": args.onebot_port, "ui": args.ui_port, "mcp": args.mcp_port}
    _assert_ports_free(ports)
    details = prepare(args.data_dir, args.astrbot_dir, ports, mode, args.owner_id)
    print(f"Sandbox prepared at {details['data_dir']} (DSH: {mode}).")
    if args.prepare_only:
        return 0
    if mode == "live":
        required = ("V2_DSH_PROVIDER", "V2_DSH_MODEL", "DEEPSEEK_API_KEY")
        missing = [key for key in required if not os.environ.get(key)]
        if missing:
            raise RuntimeError("Live DSH requires explicit environment values: " + ", ".join(missing))
    _assert_ports_free(ports)
    return _run(details, args.astrbot_dir.resolve(), ports, mode,
                owner_volume_mib=args.owner_volume_mib)


def _run(details: dict, astrbot_source: Path, ports: dict[str, int], mode: str,
         *, owner_volume_mib: int = 1024) -> int:
    data_dir = Path(details["data_dir"])
    source_python = astrbot_source / ".venv/bin/python"
    server_cmd = [str(source_python), "-m", "sandbox.server", "--data-dir", details["onebot_dir"], "--port", str(ports["ui"]), "--onebot-url", f"ws://127.0.0.1:{ports['onebot']}/ws"]
    token = Path(details["onebot_token_file"]).read_text(encoding="ascii").strip()
    home = Path(details["home"])
    env = _child_environment(dsh_enabled=False, home=home)
    env.update({"ONEBOT_ACCESS_TOKEN": token, "SANDBOX_DSH_MODE": mode,
                "SANDBOX_OWNER_ID": details["owner"]["user_id"] if details["owner"] else "",
                "SANDBOX_CONTROL_TOKEN": token,
                "SANDBOX_ARTIFACT_ROOT": str(Path(details["astrbot_root"]) / "data")})
    astrbot_env = _child_environment(dsh_enabled=mode in {"live", "scripted"},
                                     home=home, scripted=mode == "scripted")
    astrbot_env.update({"ONEBOT_ACCESS_TOKEN": token, "SANDBOX_DSH_MODE": mode,
                        "SANDBOX_ARTIFACT_ROOT": str(Path(details["astrbot_root"]) / "data"),
                        "PYTHONUNBUFFERED": "1"})
    astrbot_env.update({"ASTRBOT_ROOT": details["astrbot_root"], "ASTRBOT_SOURCE": str(astrbot_source),
                        "V2_QQ_OWNERS": json.dumps([details["owner"]] if details["owner"] else [], separators=(",", ":")),
                        "V2_QQ_ALLOWED_CONVERSATIONS": "group:91000,private:90001,private:90002" +
                            ((",private:" + details["owner"]["user_id"]) if details["owner"] else ""),
                        "V2_MCP_PORT": str(ports["mcp"]), "SANDBOX_SCENARIO_DB": details["scenario_db"],
                        "V2_EXECUTION_CONTRACT": "2", "V2_OWNER_WORKDIR": details["staged_repo"],
                        "V2_GROUP_STORAGE_MIB": "1024", "V2_GROUP_MEMORY_MIB": "512",
                        "V2_GROUP_TASKS": "64", "V2_GROUP_CPU_QUOTA": "100",
                        "V2_GROUP_MAX_COMMAND_SECONDS": "60", "V2_GROUP_NETWORK": "0"})
    astrbot_env.update({
        "SANDBOX_DSH_MODE": mode,
        "SANDBOX_OWNER_ID": details["owner"]["user_id"] if details["owner"] else "",
        "SANDBOX_CONTROL_URL": f"http://127.0.0.1:{ports['ui']}",
        "SANDBOX_CONTROL_TOKEN": token,
        "SANDBOX_PROBE_PATH": str(Path(details["staged_repo"]) / "sandbox/probe.mjs"),
    })
    # Disabled DSH mode uses the journal-only plugin set. It cannot emit free replies.
    if mode == "disabled":
        router_link = Path(details["astrbot_root"]) / "data/plugins/v2_dsh_router"
        if router_link.is_symlink():
            router_link.unlink()
    children: list[subprocess.Popen] = []
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in reversed(children):
            try:
                # Let AstrBot close ACP itself. Signalling the whole group
                # here also interrupts ACP during its shutdown requests.
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        children.append(subprocess.Popen(server_cmd, cwd=ROOT, env=env, start_new_session=True))
        # Wait for UI readiness; child exit or timeout aborts before AstrBot boot.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if children[0].poll() is not None:
                return children[0].returncode or 1
            try:
                with socket.create_connection(("127.0.0.1", ports["ui"]), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Sandbox UI did not start within 20 seconds")
        webui = astrbot_source / "data/dist"
        if not webui.is_dir():
            raise RuntimeError(f"Pinned AstrBot WebUI assets are missing: {webui}")
        children.append(subprocess.Popen([str(source_python), "main.py", "--webui-dir", str(webui)], cwd=astrbot_source, env=astrbot_env, start_new_session=True))
        while not stopping:
            for child in children:
                rc = child.poll()
                if rc is not None:
                    return rc
            time.sleep(0.2)
        return 0
    finally:
        stop()
        for child in reversed(children):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                child.poll()  # Reap the session leader before checking its process group.
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(2)
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        # Group volumes are independent of the synthetic Owner. Unmount only
        # this stopped scenario's volumes; never adopt production paths.
        import importlib.util
        helper = ROOT / "dsh/plugins/agent-profile/owner_volume.py"
        spec = importlib.util.spec_from_file_location("sandbox_volume_cleanup", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        group_root = Path(details["staged_repo"]) / ".runtime/group-storage"
        for workspace in group_root.glob("*/workspace"):
            if os.path.ismount(workspace):
                module.unmount(workspace)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"sandbox runtime: {exc}", file=sys.stderr)
        raise SystemExit(1)
