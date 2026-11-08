"""Native install of hermes-telegram-talk beside a synthetic Discord voice plugin in one disposable profile.

usage: python3 tests/coexistence/run.py   (Hermes runtime: HERMES_TALK_HERMES_PYTHON, else the first installed venv)

Installs a runtime-only Git distribution of the committed HEAD (the files a profile runs; the install scanner
blocks the repository's test scripts) through `hermes plugins install file://... --ref <sha> --enable` and checks: the
target directory is the manifest name, no legacy plugins/hermes-talk appears, the sentinel stays installed,
enabled and byte-unchanged, unrelated config survives, and in one loaded PluginManager each plugin owns its own
module, CLI command, gateway hook, platform handler and (for Talk) the Pulse ctx.llm binding.
"""
import glob, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "hermes-telegram-talk"
SENTINEL = "discord-voice-sentinel"
RUNTIME_FILES = ("plugin.yaml", "__init__.py", "server.py", "hermes_talk", "static", "LICENSES")

SENTINEL_FILES = {
    "plugin.yaml": f'name: {SENTINEL}\nversion: "0.0.1"\ndescription: "synthetic Discord voice sentinel"\nkind: standalone\n',
    "__init__.py": (
        "def _hook(event=None, **kwargs):\n    return None\n\n\n"
        "def _factory(adapter):\n    return []\n\n\n"
        "def _setup(parser):\n    return None\n\n\n"
        "def _run(args):\n    return 0\n\n\n"
        "def register(ctx):\n"
        "    ctx.register_cli_command('discord-sentinel', 'synthetic', _setup, _run)\n"
        "    ctx.register_hook('pre_gateway_dispatch', _hook)\n"
        "    ctx.register_platform_handler('discord', _factory)\n"),
}

PROBE = r'''
import json, sys
from pathlib import Path
talk_dir = Path(sys.argv[1])
sys.path.insert(0, str(talk_dir))
import hermes_talk.pulse_model as pulse_model
from hermes_cli.plugins import PluginManager
m = PluginManager()
m.discover_and_load(force=True)
listed = {p["name"]: {"enabled": bool(p.get("enabled")), "error": p.get("error")} for p in m.list_plugins()}
mods = {name: getattr(getattr(lp, "module", None), "__name__", None) for name, lp in m._plugins.items()}
hooks = {cb.__module__ for cb in m._hooks.get("pre_gateway_dispatch", [])}
handlers = {k: [getattr(e[0] if isinstance(e, tuple) else e, "__module__", repr(e)) for e in v]
            for k, v in m._platform_handler_factories.items()}
llm = pulse_model.bound()
print(json.dumps({"listed": listed, "modules": mods, "cli": {k: v.get("plugin") for k, v in m._cli_commands.items()},
                  "hooks": sorted(hooks), "handlers": handlers,
                  "llm_owner": getattr(llm, "_plugin_id", None) if llm is not None else None}))
'''


def hermes_python():
    explicit = os.environ.get("HERMES_TALK_HERMES_PYTHON")
    if explicit:
        return explicit
    found = sorted(glob.glob(str(Path.home() / ".hermes/installs/*/environments/*/venv/bin/python")))
    assert found, "no Hermes runtime; set HERMES_TALK_HERMES_PYTHON"
    return found[0]


def digest(path):
    path = Path(path)
    files = sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts) if path.is_dir() else [path]
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(path) if path.is_dir() else p.name).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def main():
    python = hermes_python()
    hermes = str(Path(python).parent / "hermes")
    rev = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    tmp = Path(tempfile.mkdtemp(prefix="talk-coexist-"))
    assert "/.hermes/profiles/" not in str(tmp), "TMPDIR must not be under a real profile"
    try:
        dist = tmp / "dist"
        dist.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", rev, *RUNTIME_FILES], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(dist)], input=archive.stdout, check=True)
        git = lambda *a: subprocess.run(["git", "-C", str(dist), "-c", "user.name=talk", "-c", "user.email=talk@localhost", *a],
                                        check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", f"hermes-telegram-talk runtime from {rev}")
        dist_rev = git("rev-parse", "HEAD")
        home = tmp / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
               "TMPDIR": str(tmp)}
        native = lambda *a: subprocess.run([hermes, *a], cwd=tmp, env=env, capture_output=True, text=True, timeout=600,
                                           stdin=subprocess.DEVNULL)
        out = native("profile", "create", "synthetic", "--no-alias", "--no-skills")
        assert out.returncode == 0, out.stderr[-600:]
        profile = home / ".hermes" / "profiles" / "synthetic"
        sentinel = profile / "plugins" / SENTINEL
        sentinel.mkdir(parents=True)
        for name, text in SENTINEL_FILES.items():
            (sentinel / name).write_text(text)
        (profile / "config.yaml").write_text(
            f"display:\n  coexistence_marker: kept\nplugins:\n  enabled:\n    - {SENTINEL}\n")
        before = digest(sentinel)

        out = native("-p", "synthetic", "plugins", "install", f"file://{dist}", "--ref", dist_rev, "--enable", "--yes-deps")
        assert out.returncode == 0, (out.stdout[-800:], out.stderr[-800:])
        target = profile / "plugins" / PLUGIN_ID
        assert (target / "plugin.yaml").read_text().splitlines()[0] == f"name: {PLUGIN_ID}"
        assert not (profile / "plugins" / "hermes-talk").exists(), "the legacy plugin directory must not appear"
        for name in RUNTIME_FILES:
            assert digest(target / name) == digest(ROOT / name), f"installed {name} differs from the checkout"
        assert digest(sentinel) == before, "the Discord sentinel changed"

        listed = [line.split() for line in native("-p", "synthetic", "plugins", "list", "--plain", "--no-bundled").stdout.splitlines()]
        assert ["enabled", "git", f"pinned@{dist_rev[:8]}", "0.2.0", PLUGIN_ID] in listed, listed
        assert any(row[:1] == ["enabled"] and row[-1] == SENTINEL for row in listed), listed
        enabled = native("-p", "synthetic", "config", "get", "plugins.enabled")
        marker = native("-p", "synthetic", "config", "get", "display.coexistence_marker")
        assert PLUGIN_ID in enabled.stdout and SENTINEL in enabled.stdout, enabled.stdout
        assert "kept" in marker.stdout, marker.stdout

        probe = subprocess.run([python, "-c", PROBE, str(target)], cwd=tmp, capture_output=True, text=True, timeout=180,
                               env={**env, "HERMES_HOME": str(profile)})
        assert probe.returncode == 0, probe.stderr[-1200:]
        state = json.loads(probe.stdout.strip().splitlines()[-1])
        for name in (PLUGIN_ID, SENTINEL):
            assert state["listed"].get(name) == {"enabled": True, "error": None}, state["listed"]
        assert state["modules"][PLUGIN_ID] != state["modules"][SENTINEL], state["modules"]
        assert state["cli"].get("talk") == PLUGIN_ID and state["cli"].get("discord-sentinel") == SENTINEL, state["cli"]
        talk_mod, sentinel_mod = state["modules"][PLUGIN_ID], state["modules"][SENTINEL]
        assert set(state["hooks"]) == {talk_mod, sentinel_mod}, state["hooks"]
        assert state["handlers"].get("telegram") == [talk_mod], state["handlers"]
        assert state["handlers"].get("discord") == [sentinel_mod], state["handlers"]
        assert state["llm_owner"] == PLUGIN_ID, state["llm_owner"]
        print(json.dumps({"revision": rev, "distribution": dist_rev, "installed": PLUGIN_ID, "sentinel": SENTINEL, **state}, sort_keys=True))
        print("TALK_COEXISTENCE_OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
