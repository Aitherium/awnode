"""awnode local sandbox: confinement flags, records, engine detection, MCP opt-in.

Every test except the last runs against a fake engine runner that records argv;
nothing here starts a container. The live test runs only with AWNODE_SANDBOX_LIVE=1.
"""

import json
import os

import pytest

from awnode import sandbox as sb


class FakeEngine:
    """Records every argv; answers like podman would for a confined container."""

    def __init__(self, *, engines=("podman",), inspect_override=None, ps_state="running",
                 image_present=True, wsl_distros="", wsl_podman=()):
        self.calls = []
        self.engines = set(engines)
        self.inspect_override = inspect_override or {}
        self.ps_state = ps_state
        self.image_present = image_present
        self.wsl_distros = wsl_distros
        self.wsl_podman = set(wsl_podman)
        self.created = {}
        # what podman really shows: EffectiveCaps omitted from JSON, CapDrop expanded
        self.proc_status = ("Name:\tsleep\nCapPrm:\t0000000000000000\n"
                            "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n"
                            "NoNewPrivs:\t1\n")

    def inspect_doc(self, name):
        doc = {
            "Name": name,
            "Config": {"User": "65534:65534"},
            "HostConfig": {
                "CapDrop": ["CAP_CHOWN", "CAP_KILL", "CAP_SETUID"], "CapAdd": [],
                "Privileged": False, "ReadonlyRootfs": True,
                "SecurityOpt": ["no-new-privileges"], "PidsLimit": 256,
                "Memory": 2147483648, "NanoCpus": 2000000000,
                "NetworkMode": "none", "PortBindings": {},
            },
            "Mounts": [{"Type": "bind", "Destination": "/work", "RW": True}],
        }
        for k, v in self.inspect_override.items():
            if k in ("Config", "HostConfig"):
                doc[k].update(v)
            else:
                doc[k] = v
        return doc

    def __call__(self, argv, timeout=None, stream=False):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:1] == ["wsl"] and argv[1:3] == ["-l", "-q"]:
            return sb.RunResult(0 if self.wsl_distros else 1,
                                "\x00".join(self.wsl_distros) if self.wsl_distros else "")
        if argv[:1] == ["wsl"]:
            distro = argv[2]
            if distro not in self.wsl_podman:
                return sb.RunResult(127, "", "podman: not found")
            argv = argv[5:]  # strip: wsl -d <distro> --exec podman
        else:
            if argv[0] not in self.engines:
                return sb.RunResult(127, "", f"not found: {argv[0]}")
            argv = argv[1:]
        verb = argv[0]
        if verb == "info":
            return sb.RunResult(0, "ok")
        if verb == "image":
            return sb.RunResult(0 if self.image_present else 1, "[]")
        if verb == "pull":
            self.image_present = True
            return sb.RunResult(0)
        if verb == "run":
            name = argv[argv.index("--name") + 1]
            self.created[name] = True
            return sb.RunResult(0, "cid\n")
        if verb == "inspect":
            return sb.RunResult(0, json.dumps([self.inspect_doc(argv[1])]))
        if verb == "ps":
            lines = [f"{n}\t{self.ps_state}" for n in self.created]
            return sb.RunResult(0, "\n".join(lines))
        if verb == "exec" and argv[-2:] == ["cat", "/proc/1/status"]:
            return sb.RunResult(0, self.proc_status)
        if verb == "exec":
            return sb.RunResult(7 if "fail" in argv else 0, "4\n")
        if verb in ("rm", "stop", "start"):
            if verb == "rm":
                self.created.pop(argv[-1], None)
            return sb.RunResult(0)
        return sb.RunResult(1, "", f"unexpected {argv}")

    def runs(self):
        return [c for c in self.calls if "run" in c and "--name" in c]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / ".aither"))
    monkeypatch.delenv("AWNODE_SANDBOX_ENGINE", raising=False)
    monkeypatch.delenv("AWNODE_MCP_ALLOW_SANDBOX", raising=False)
    return tmp_path / ".aither"


_REAL_RUNNER = sb.default_runner


@pytest.fixture()
def fake(monkeypatch):
    f = FakeEngine()
    monkeypatch.setattr(sb, "default_runner", f)
    return f



def _create(fake, **kw):
    return sb.create(runner=fake, engine=sb.Engine("podman", "podman", ["podman"]),
                     stream=False, **kw)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_every_confinement_flag_is_on_the_run(fake):
    rec = _create(fake, name="t1")
    (argv,) = fake.runs()
    assert _flag(argv, "--cap-drop") == "ALL"
    assert _flag(argv, "--security-opt") == "no-new-privileges"
    assert _flag(argv, "--user") == "65534:65534"
    assert "--read-only" in argv
    assert _flag(argv, "--tmpfs") == "/tmp:rw,size=256m"
    assert _flag(argv, "--pids-limit") == "256"
    assert _flag(argv, "--memory") == "2g"
    assert _flag(argv, "--cpus") == "2"
    assert _flag(argv, "--label") == f"aither.sandbox={rec.id}"
    mount = _flag(argv, "--mount")
    assert mount == f"type=bind,src={rec.workspace},dst=/work"
    assert rec.workspace.endswith(os.path.join("sandboxes", rec.id, "work"))
    # the only mount flag is the workspace one
    assert argv.count("--mount") == 1 and "-v" not in argv and "--volume" not in argv


def test_network_none_is_the_default_and_net_is_recorded(fake):
    rec = _create(fake, name="t1")
    assert _flag(fake.runs()[0], "--network") == "none"
    assert rec.network == "none"
    fake.inspect_override = {"HostConfig": {"NetworkMode": "bridge"}}
    rec2 = _create(fake, name="t2", net=True)
    assert _flag(fake.runs()[1], "--network") == "bridge"
    assert rec2.network == "bridge"


def test_no_publish_flag_ever_appears(fake):
    _create(fake, name="t1")
    _create(fake, name="t2", net=True)
    for argv in fake.calls:
        for bad in ("-p", "--publish", "-P", "--publish-all", "--privileged", "--cap-add"):
            assert bad not in argv, (bad, argv)
        assert not any(a.startswith("--publish") for a in argv)


def test_publish_flags_are_refused_by_the_guard():
    for bad in (["podman", "run", "-p", "8080:80"], ["podman", "run", "--publish=80"],
                ["podman", "run", "--privileged"], ["podman", "run", "--network", "host"],
                ["podman", "run", "--network=host"], ["podman", "run", "--cap-add", "SYS_ADMIN"]):
        with pytest.raises(sb.SandboxError):
            sb.assert_safe_run_argv(bad)


def test_a_flag_the_engine_did_not_enforce_fails_the_create(fake, home):
    fake.inspect_override = {"HostConfig": {"ReadonlyRootfs": False, "PidsLimit": 0}}
    with pytest.raises(sb.ConfinementError) as ei:
        _create(fake, name="t1")
    assert ei.value.exit_code == 5
    assert any("read-only" in p for p in ei.value.problems)
    assert any("pids" in p for p in ei.value.problems)
    sid = _flag(fake.runs()[0], "--label").split("=")[1]
    assert ["podman", "rm", "-f", "aither-sb-" + sid] in fake.calls
    assert sb.load_records() == []


def test_kernel_caps_from_inside_are_the_verdict(fake):
    fake.proc_status = fake.proc_status.replace("CapEff:\t0000000000000000",
                                                "CapEff:\t00000000a80425fb")
    with pytest.raises(sb.ConfinementError) as ei:
        _create(fake, name="t1")
    assert any("CapEff" in p for p in ei.value.problems)


def test_published_port_or_extra_mount_fails_verification():
    eng = FakeEngine()
    doc = eng.inspect_doc("x")
    assert sb.verify_confinement(doc, net=False, proc_status=eng.proc_status) == []
    # podman expands CapDrop and omits EffectiveCaps: without the kernel view, refuse
    assert sb.verify_confinement(doc, net=False)
    doc["EffectiveCaps"] = []
    assert sb.verify_confinement(doc, net=False) == []
    doc["HostConfig"]["PortBindings"] = {"80/tcp": [{"HostPort": "8080"}]}
    doc["Mounts"].append({"Destination": "/data", "RW": True})
    probs = sb.verify_confinement(doc, net=False)
    assert any("published" in p for p in probs) and any("/data" in p for p in probs)
    docker_doc = eng.inspect_doc("y")
    docker_doc.pop("EffectiveCaps", None)
    docker_doc["HostConfig"]["CapDrop"] = ["ALL"]
    assert sb.verify_confinement(docker_doc, net=False) == []
    docker_doc["HostConfig"]["CapDrop"] = ["CAP_CHOWN"]
    assert sb.verify_confinement(docker_doc, net=False)


def test_record_round_trips_and_reconciles(fake, home):
    rec = _create(fake, name="t1")
    raw = json.loads((home / "sandboxes.json").read_text(encoding="utf-8"))
    assert raw["sandboxes"][0]["id"] == rec.id
    (back,) = sb.load_records()
    assert back == rec
    d = back.to_dict()
    for k in ("id", "name", "owner", "where", "engine", "image", "network", "status",
              "workspace", "created", "ttl_hours"):
        assert k in d
    assert d["owner"] == "local" and d["where"] == "local" and d["ttl_hours"] == 24
    assert "@sha256:" in d["image"]
    fake.ps_state = "exited"
    out = sb.listing(runner=fake)
    assert out["sandboxes"][0]["status"] == "stopped"
    fake.created.clear()
    assert sb.listing(runner=fake)["sandboxes"][0]["status"] == "missing"


def test_run_executes_inside_the_container_and_returns_its_exit_code(fake):
    rec = _create(fake, name="t1")
    res = sb.exec_in("t1", ["python3", "-c", "print(2+2)"], runner=fake, stream=False)
    assert res.returncode == 0 and res.stdout.strip() == "4"
    assert fake.calls[-1] == ["podman", "exec", "--workdir", "/work", rec.container,
                              "python3", "-c", "print(2+2)"]
    assert sb.exec_in(rec.id, ["fail"], runner=fake, stream=False).returncode == 7


def test_rm_removes_container_and_keeps_work_unless_asked(fake):
    rec = _create(fake, name="t1")
    out = sb.remove("t1", runner=fake)
    assert out["workspace_deleted"] is False and os.path.isdir(rec.workspace)
    assert ["podman", "rm", "-f", rec.container] in fake.calls
    assert sb.load_records() == []
    rec2 = _create(fake, name="t2")
    out = sb.remove("t2", delete_workspace=True, runner=fake)
    assert out["workspace_deleted"] is True and not os.path.exists(rec2.workspace)


def test_missing_engine_exits_4_with_next_step(monkeypatch, capsys):
    none = FakeEngine(engines=())
    with pytest.raises(sb.NoEngine) as ei:
        sb.detect_engine(none, os_name="posix")
    assert ei.value.exit_code == 4 and "podman" in ei.value.next_step
    with pytest.raises(sb.NoEngine) as ei:
        sb.detect_engine(none, os_name="nt")
    assert "WSL" in ei.value.next_step

    from awnode import cli
    monkeypatch.setattr(sb, "detect_engine",
                        lambda *a, **k: (_ for _ in ()).throw(
                            sb.NoEngine("no engine", next_step=sb.no_engine_next_step("nt"))))
    assert cli.sandbox_main(["new", "--name", "t1"]) == 4
    err = capsys.readouterr().err
    assert "next: turn on WSL" in err


def test_detection_order_docker_then_podman_then_wsl():
    both = FakeEngine(engines=("docker", "podman"))
    assert sb.detect_engine(both, os_name="posix").label == "docker"
    only_podman = FakeEngine(engines=("podman",))
    assert sb.detect_engine(only_podman, os_name="posix").label == "podman"
    assert only_podman.calls[0][:2] == ["docker", "info"]


def test_wsl_detection_builds_wsl_d_awnix_exec_podman(tmp_path):
    f = FakeEngine(engines=(), wsl_distros="\ufeffUbuntu\r\nawnix\r\n",
                   wsl_podman=("awnix", "Ubuntu"))
    eng = sb.detect_engine(f, os_name="nt")
    assert eng.label == "wsl:awnix/podman"        # awnix first even when listed second
    assert eng.prefix == ["wsl", "-d", "awnix", "--exec", "podman"]
    assert ["wsl", "-d", "awnix", "--exec", "podman", "info"] in f.calls
    # `wsl -- cmd` goes through the distro shell; --exec passes argv untouched.
    assert not any(c[:4] == ["wsl", "-d", "awnix", "--"] for c in f.calls)
    assert eng.host_path(sb.Path("C:\\Users\\me\\.aither\\sandboxes\\sb1\\work")) \
        == "/mnt/c/Users/me/.aither/sandboxes/sb1/work"
    other = FakeEngine(engines=(), wsl_distros="Ubuntu\r\nawnix", wsl_podman=("Ubuntu",))
    assert sb.detect_engine(other, os_name="nt").label == "wsl:Ubuntu/podman"
    with pytest.raises(sb.SandboxError):
        eng.host_path(sb.Path("relative/work"))
    rebuilt = sb.engine_from_label("wsl:awnix/podman")
    assert rebuilt.cmd("ps") == ["wsl", "-d", "awnix", "--exec", "podman", "ps"]


def test_bad_names_and_images_are_refused(fake):
    with pytest.raises(sb.SandboxError):
        _create(fake, name="--privileged")
    with pytest.raises(sb.SandboxError):
        _create(fake, name="t1", image="--privileged")
    assert fake.runs() == []


def test_journey_fact_is_best_effort(home):
    rec = sb.SandboxRecord(id="sb1", name="t1", engine="podman", image="i@sha256:x",
                           workspace="w", created="2026-11-13T00:00:00+00:00")
    assert sb.post_journey_fact(rec) is False          # not signed in: no call
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(
        {"active_profile": "portal", "profiles": {"portal": {"access_token": "tok"}}}))
    seen = []

    def opener_404(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    assert sb.post_journey_fact(rec, opener=opener_404) is False
    url, auth, body = seen[0]
    assert url.endswith("/api/v1/me/journey/facts") and auth == "Bearer tok"
    assert body["rung"] == "own_sandbox"


def _registry(monkeypatch, allow):
    import awnode.mcp_server as ms
    if allow:
        monkeypatch.setenv("AWNODE_MCP_ALLOW_SANDBOX", "1")
    else:
        monkeypatch.delenv("AWNODE_MCP_ALLOW_SANDBOX", raising=False)
    return ms._discover_tools()


def test_mcp_exec_tools_absent_without_opt_in(monkeypatch):
    reg = _registry(monkeypatch, allow=False)
    assert "local_sandbox_list" in reg and reg["local_sandbox_list"]["scope"] == "shell_local"
    for name in ("local_sandbox_open", "local_sandbox_exec", "local_sandbox_close"):
        assert name not in reg


def test_mcp_exec_tools_present_with_opt_in(monkeypatch, home):
    reg = _registry(monkeypatch, allow=True)
    for name in ("local_sandbox_open", "local_sandbox_exec", "local_sandbox_close"):
        assert name in reg
    monkeypatch.delenv("AWNODE_MCP_ALLOW_SANDBOX")
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("sandbox:\n  allow_mcp: true\n")
    assert sb.mcp_allowed() is True
    (home / "config.yaml").write_text("sandbox:\n  allow_mcp: yes-please\n")
    assert sb.mcp_allowed() is False


def test_mcp_exec_runs_in_the_container(fake, monkeypatch):
    from awnode.tools import sandbox as tools
    rec = _create(fake, name="t1")
    out = json.loads(tools.local_sandbox_exec("t1", "echo hi; id"))
    assert out["exit_code"] == 0
    assert fake.calls[-1] == ["podman", "exec", "--workdir", "/work", rec.container,
                              "sh", "-c", "echo hi; id"]
    listed = json.loads(tools.local_sandbox_list())
    assert listed["ok"] and listed["sandboxes"][0]["name"] == "t1"


@pytest.mark.skipif(os.environ.get("AWNODE_SANDBOX_LIVE") != "1",
                    reason="live engine test: set AWNODE_SANDBOX_LIVE=1")
def test_live_round_trip(home):
    rec = sb.create(name="livet", runner=_REAL_RUNNER, stream=True)
    try:
        r = sb.exec_in("livet", ["python3", "-c", "print(2+2)"], runner=_REAL_RUNNER,
                       stream=False)
        assert r.returncode == 0 and r.stdout.strip() == "4"
        r = sb.exec_in("livet", ["sh", "-c", "getent hosts example.com || echo NO-NET"],
                       runner=_REAL_RUNNER, stream=False)
        assert "NO-NET" in r.stdout
    finally:
        sb.remove(rec.id, delete_workspace=True, runner=_REAL_RUNNER)
