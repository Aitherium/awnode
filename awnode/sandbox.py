"""
awnode local sandbox -- your own locked-down container, on your own machine
===========================================================================

``awnode sandbox new|run|ls|stop|rm`` makes a throwaway container for testing
code, projects and agents. It runs on the user's hardware (docker, podman, or
podman inside a WSL distro such as awnix), costs nothing and needs no fleet.

Confinement is not optional. Every sandbox is created with ALL of:

    --cap-drop ALL  --security-opt no-new-privileges  --user 65534:65534
    --read-only  --tmpfs /tmp:rw,size=256m  --pids-limit 256  --memory 2g
    --cpus 2  --network none  (egress only with an explicit --net)

No port is ever published. The only writable mount is the sandbox's own
workspace, ``~/.aither/sandboxes/<id>/work`` at ``/work``. After the engine
creates the container we read it back with ``inspect`` and refuse (remove the
container, exit 5) if any of those limits did not take: a flag the engine
cannot enforce fails the create, it is never silently dropped. That is the
awrun confine principle, copied here as a check rather than imported.

The engine is detected in order: ``docker info`` -> ``podman info`` -> on
Windows, ``wsl -l -q`` then ``wsl -d <distro> --exec podman info`` (awnix first,
then any distro that has podman). ``--exec`` and not ``--``: ``wsl -- cmd``
re-parses the command through the distro's login shell, so quoting breaks and
a ``;`` in an argument runs a second command. ``--exec`` passes argv as-is.

State lives in ``~/.aither/sandboxes.json`` (written atomically) and is
reconciled against ``ps -a --filter label=aither.sandbox`` on every read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger("awnode.sandbox")

# Pinned BY DIGEST. Resolved 2026-11-13 with the anonymous ghcr registry API
# (token -> HEAD /v2/aitherium/aitheros-dev-workspace/manifests/latest):
# OCI image index, linux/amd64 manifest sha256:43f68bb0..., 21 layers,
# 1722.8 MB compressed (about 3.7 GB unpacked). Built 2026-11-13T02:25Z.
DEFAULT_IMAGE = (
    "ghcr.io/aitherium/aitheros-dev-workspace"
    "@sha256:d1f6b89628b6d987493f158c6d4b2744538519076f498a23fbb9eb9c54ef9c4c"
)

LABEL = "aither.sandbox"
CONTAINER_PREFIX = "aither-sb-"
SANDBOX_UID = "65534:65534"
PIDS_LIMIT = 256
MEMORY = "2g"
MEMORY_BYTES = 2 * 1024 ** 3
CPUS = 2
TMPFS = "/tmp:rw,size=256m"
WORKDIR = "/work"
DEFAULT_TTL_HOURS = 24

EXIT_NOT_FOUND = 2
EXIT_ENGINE = 1
EXIT_NO_ENGINE = 4
EXIT_CONFINEMENT = 5

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]*$")

#: Flags that would widen the box. They must never reach an engine `run`.
_FORBIDDEN_FLAGS = (
    "-p", "--publish", "-P", "--publish-all", "--privileged", "--cap-add",
    "--pid", "--ipc", "--uts", "--userns", "--device", "-v", "--volume",
    "--volumes-from",
)


class SandboxError(Exception):
    """A sandbox operation failed. ``exit_code`` is what the CLI returns."""

    exit_code = EXIT_ENGINE

    def __init__(self, message: str, next_step: str = "") -> None:
        super().__init__(message)
        self.next_step = next_step


class NoEngineError(SandboxError):
    exit_code = EXIT_NO_ENGINE


NoEngine = NoEngineError  # the name the CLI/spec use


class ConfinementError(SandboxError):
    exit_code = EXIT_CONFINEMENT

    def __init__(self, problems: List[str]) -> None:
        super().__init__(
            "the engine did not enforce every sandbox limit: " + "; ".join(problems),
            next_step="this engine cannot run a confined sandbox; try podman "
                      "(or WSL podman on Windows)",
        )
        self.problems = problems


class SandboxNotFoundError(SandboxError):
    exit_code = EXIT_NOT_FOUND


NotFound = SandboxNotFoundError


# ---------------------------------------------------------------------------
# Running engine commands
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


#: runner(argv, timeout=..., stream=...) -> RunResult. Tests pass a fake.
Runner = Callable[..., RunResult]


def default_runner(argv: Sequence[str], *, timeout: Optional[float] = None,
                   stream: bool = False) -> RunResult:
    """Run an engine command. ``stream`` inherits stdout/stderr (CLI use)."""
    kw: Dict = {"stdin": subprocess.DEVNULL, "timeout": timeout}
    if os.name == "nt" and not stream:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if stream:
            p = subprocess.run(list(argv), **kw)
            return RunResult(p.returncode)
        p = subprocess.run(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", **kw)
        return RunResult(p.returncode, p.stdout or "", p.stderr or "")
    except FileNotFoundError:
        return RunResult(127, "", f"not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"timed out after {timeout}s")


@dataclass
class Engine:
    kind: str                 # "docker" | "podman"
    label: str                # "docker" | "podman" | "wsl:awnix/podman"
    prefix: List[str]
    wsl_distro: str = ""

    def cmd(self, *args: str) -> List[str]:
        return [*self.prefix, *args]

    def host_path(self, p: Path) -> str:
        """The workspace path as the engine sees it."""
        return to_wsl_path(p) if self.wsl_distro else str(p)


def _wsl_prefix(distro: str) -> List[str]:
    return ["wsl", "-d", distro, "--exec", "podman"]


def engine_from_label(label: str) -> Engine:
    """Rebuild the engine a sandbox was created on from its record."""
    if label.startswith("wsl:"):
        distro = label[4:].split("/", 1)[0]
        return Engine("podman", label, _wsl_prefix(distro), wsl_distro=distro)
    if label in ("docker", "podman"):
        return Engine(label, label, [label])
    raise SandboxError(f"unknown engine {label!r} on the sandbox record")


def to_wsl_path(p: Path) -> str:
    """C:\\Users\\x\\.aither -> /mnt/c/Users/x/.aither (AWNODE_WSL_MOUNT_ROOT overrides /mnt)."""
    s = str(p).replace("\\", "/")
    if s.startswith("//?/"):
        s = s[4:]
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    if not m:
        if s.startswith("/"):
            return s  # already a Linux path
        raise SandboxError(f"cannot map {p} into WSL (not a drive path)",
                           next_step="set AITHER_HOME to a folder on a local drive, e.g. C:\\")
    root = os.environ.get("AWNODE_WSL_MOUNT_ROOT", "/mnt").rstrip("/")
    return f"{root}/{m.group(1).lower()}/{m.group(2)}"


def _wsl_distros(runner: Runner) -> List[str]:
    r = runner(["wsl", "-l", "-q"], timeout=30)
    if r.returncode != 0:
        return []
    # wsl.exe prints UTF-16LE; decoded as UTF-8 it carries NULs (and maybe a BOM).
    text = (r.stdout or "").replace("\x00", "").replace("\ufeff", "")
    names = [ln.strip() for ln in text.splitlines() if ln.strip()]
    names = [n for n in names if not n.lower().startswith("docker-desktop")]
    names.sort(key=lambda n: (n.lower() != "awnix",))
    return names


def no_engine_next_step(os_name: str = os.name) -> str:
    if os_name == "nt":
        return "turn on WSL, then run awnode sandbox new again"
    return "install podman (or docker), then run awnode sandbox new again"


def detect_engine(runner: Optional[Runner] = None, *, os_name: str = os.name) -> Engine:
    """docker -> podman -> (Windows) WSL distro podman, awnix first."""
    runner = runner or default_runner
    forced = os.environ.get("AWNODE_SANDBOX_ENGINE", "").strip()
    if forced:
        eng = engine_from_label(forced if not forced.startswith("wsl:") or "/" in forced
                                else forced + "/podman")
        if runner(eng.cmd("info"), timeout=60).returncode == 0:
            return eng
        raise NoEngine(f"AWNODE_SANDBOX_ENGINE={forced} is not answering",
                       next_step=no_engine_next_step(os_name))
    for kind in ("docker", "podman"):
        if runner([kind, "info"], timeout=30).returncode == 0:
            return Engine(kind, kind, [kind])
    if os_name == "nt":
        for distro in _wsl_distros(runner):
            if runner(_wsl_prefix(distro) + ["info"], timeout=90).returncode == 0:
                return Engine("podman", f"wsl:{distro}/podman", _wsl_prefix(distro),
                              wsl_distro=distro)
    raise NoEngine("no container engine found (tried docker, podman"
                   + (", WSL podman" if os_name == "nt" else "") + ")",
                   next_step=no_engine_next_step(os_name))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def aither_home() -> Path:
    return Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))


def store_path() -> Path:
    return aither_home() / "sandboxes.json"


def sandboxes_root() -> Path:
    return aither_home() / "sandboxes"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SandboxRecord:
    id: str
    name: str
    engine: str
    image: str
    workspace: str
    created: str
    network: str = "none"
    status: str = "running"
    owner: str = "local"
    where: str = "local"
    ttl_hours: int = DEFAULT_TTL_HOURS
    container: str = ""

    def __post_init__(self) -> None:
        if not self.container:
            self.container = CONTAINER_PREFIX + self.id

    @property
    def expires(self) -> datetime:
        created = datetime.fromisoformat(self.created)
        return datetime.fromtimestamp(created.timestamp() + self.ttl_hours * 3600,
                                      tz=timezone.utc)

    def expired(self, now: Optional[datetime] = None) -> bool:
        return (now or _now()) >= self.expires

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["expires"] = self.expires.isoformat(timespec="seconds")
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "SandboxRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_records() -> List[SandboxRecord]:
    p = store_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [SandboxRecord.from_dict(d) for d in data.get("sandboxes", [])
            if isinstance(d, dict) and d.get("id")]


def save_records(records: List[SandboxRecord]) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"version": 1, "sandboxes": [asdict(r) for r in records]},
                      indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".sandboxes.", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as exc:
            logger.warning("could not remove temp file %s: %s", tmp, exc)
        raise


def find(ident: str, records: Optional[List[SandboxRecord]] = None) -> SandboxRecord:
    records = load_records() if records is None else records
    for r in records:
        if ident in (r.id, r.name, r.container):
            return r
    raise NotFound(f"no sandbox called {ident!r}",
                   next_step="awnode sandbox ls  (or: awnode sandbox new)")


def reconcile(records: List[SandboxRecord], runner: Optional[Runner] = None
              ) -> Dict[str, List[str]]:
    """Refresh each record's status from the engine. Returns {engine: [orphan names]}."""
    runner = runner or default_runner
    orphans: Dict[str, List[str]] = {}
    for label in sorted({r.engine for r in records}):
        try:
            eng = engine_from_label(label)
        except SandboxError:
            continue
        res = runner(eng.cmd("ps", "-a", "--filter", f"label={LABEL}",
                             "--format", "{{.Names}}\t{{.State}}"), timeout=60)
        if res.returncode != 0:
            for r in records:
                if r.engine == label:
                    r.status = "engine_unreachable"
            continue
        states: Dict[str, str] = {}
        for ln in res.stdout.splitlines():
            parts = ln.strip().split("\t")
            if parts and parts[0]:
                states[parts[0]] = (parts[1] if len(parts) > 1 else "").lower()
        mine = set()
        for r in records:
            if r.engine != label:
                continue
            mine.add(r.container)
            st = states.get(r.container)
            if st is None:
                r.status = "missing"
            elif r.expired():
                r.status = "expired"
            elif st.startswith("running") or st.startswith("up"):
                r.status = "running"
            else:
                r.status = "stopped"
        extra = sorted(n for n in states if n not in mine)
        if extra:
            orphans[label] = extra
    return orphans


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------

def run_argv(rec: SandboxRecord, engine: Engine, *, net: bool) -> List[str]:
    """The one `run` command a sandbox is created with."""
    ws = engine.host_path(Path(rec.workspace))
    if "," in ws:
        raise SandboxError(f"workspace path has a comma, which --mount cannot carry: {ws}",
                           next_step="set AITHER_HOME to a path without commas")
    argv = engine.cmd(
        "run", "-d",
        "--name", rec.container,
        "--label", f"{LABEL}={rec.id}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", SANDBOX_UID,
        "--read-only",
        "--tmpfs", TMPFS,
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", MEMORY,
        "--cpus", str(CPUS),
        "--network", "bridge" if net else "none",
        "--mount", f"type=bind,src={ws},dst={WORKDIR}",
        "--workdir", WORKDIR,
        "--env", f"HOME={WORKDIR}",
        # The image's own entrypoint clones repos and writes $HOME config; a
        # sandbox is a quiet box that ends itself when its TTL runs out.
        "--entrypoint", "sleep",
        rec.image,
        str(int(rec.ttl_hours * 3600)),
    )
    assert_safe_run_argv(argv)
    return argv


def assert_safe_run_argv(argv: Sequence[str]) -> None:
    """Refuse any argv that would publish a port or widen the box."""
    for a in argv:
        head = a.split("=", 1)[0]
        if a in _FORBIDDEN_FLAGS or head in ("--publish", "--cap-add", "--privileged",
                                             "--device", "--volume"):
            raise SandboxError(f"refusing to pass {a!r} to the engine")
        if a.startswith("--network=") and a != "--network=none":
            raise SandboxError(f"refusing {a!r}")
    for i, a in enumerate(argv[:-1]):
        if a == "--network" and argv[i + 1] not in ("none", "bridge"):
            raise SandboxError(f"refusing --network {argv[i + 1]!r}")


def _proc_caps(proc_status: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ln in proc_status.splitlines():
        k, _, v = ln.partition(":")
        if k in ("CapEff", "CapPrm", "CapBnd", "NoNewPrivs"):
            out[k] = v.strip()
    return out


def verify_confinement(info: Dict, *, net: bool, proc_status: str = "") -> List[str]:
    """Read back an `inspect` result; every limit that did not take is a problem.

    ``proc_status`` is ``/proc/1/status`` read from inside the box. When present,
    the kernel's own CapEff/CapBnd/NoNewPrivs are the verdict on capabilities
    (docker reports CapDrop ["ALL"]; podman expands it to the default list, and
    its JSON leaves EffectiveCaps out when it is empty).
    """
    hc = info.get("HostConfig") or {}
    cfg = info.get("Config") or {}
    problems: List[str] = []
    cap_drop = [str(c).upper() for c in (hc.get("CapDrop") or [])]
    cap_add = hc.get("CapAdd") or []
    caps = _proc_caps(proc_status) if proc_status else {}
    if caps:
        for k in ("CapEff", "CapPrm", "CapBnd"):
            if caps.get(k) and int(caps[k], 16) != 0:
                problems.append(f"{k} is {caps[k]} inside the box, not 0")
        if caps.get("NoNewPrivs") not in (None, "1"):
            problems.append("NoNewPrivs is not 1 inside the box")
    else:
        eff = info.get("EffectiveCaps")
        if not ("ALL" in cap_drop or "CAP_ALL" in cap_drop or eff == []):
            problems.append(f"capabilities not all dropped (CapDrop={cap_drop})")
    if cap_add:
        problems.append(f"capabilities added: {cap_add}")
    if hc.get("Privileged"):
        problems.append("container is privileged")
    if hc.get("ReadonlyRootfs") is not True:
        problems.append("root filesystem is not read-only")
    if not any("no-new-privileges" in str(s) for s in (hc.get("SecurityOpt") or [])):
        problems.append("no-new-privileges is not set")
    if str(cfg.get("User", "")) != SANDBOX_UID:
        problems.append(f"user is {cfg.get('User')!r}, not {SANDBOX_UID}")
    if int(hc.get("PidsLimit") or 0) != PIDS_LIMIT:
        problems.append(f"pids limit is {hc.get('PidsLimit')}, not {PIDS_LIMIT}")
    if int(hc.get("Memory") or 0) != MEMORY_BYTES:
        problems.append(f"memory limit is {hc.get('Memory')}, not {MEMORY_BYTES}")
    if int(hc.get("NanoCpus") or 0) != CPUS * 10 ** 9:
        problems.append(f"cpu limit is {hc.get('NanoCpus')}, not {CPUS * 10 ** 9}")
    mode = str(hc.get("NetworkMode", ""))
    if not net and mode != "none":
        problems.append(f"network is {mode!r}, not 'none'")
    if net and (mode == "host" or mode.startswith("container:")):
        problems.append(f"network {mode!r} is not allowed")
    ports = hc.get("PortBindings") or {}
    if any(v for v in ports.values()) or hc.get("PublishAllPorts"):
        problems.append(f"ports are published: {ports}")
    mounts = info.get("Mounts") or []
    rw_other = [m.get("Destination") for m in mounts
                if m.get("Destination") != WORKDIR and m.get("RW", True)]
    if rw_other:
        problems.append(f"extra writable mounts: {rw_other}")
    if not any(m.get("Destination") == WORKDIR for m in mounts):
        problems.append(f"workspace is not mounted at {WORKDIR}")
    return problems


def _inspect(engine: Engine, name: str, runner: Runner) -> Dict:
    r = runner(engine.cmd("inspect", name), timeout=60)
    if r.returncode != 0:
        raise SandboxError(f"inspect {name} failed: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout or "[]")
    return data[0] if isinstance(data, list) and data else (data or {})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _next_name(records: List[SandboxRecord]) -> str:
    used = {r.name for r in records}
    n = 1
    while f"t{n}" in used:
        n += 1
    return f"t{n}"


def create(name: str = "", image: str = "", *, net: bool = False,
           runner: Optional[Runner] = None, engine: Optional[Engine] = None,
           stream: bool = True, ttl_hours: int = DEFAULT_TTL_HOURS) -> SandboxRecord:
    """Make one confined sandbox. Raises NoEngine / ConfinementError / SandboxError."""
    runner = runner or default_runner
    records = load_records()
    if records:
        reconcile(records, runner)
    name = (name or _next_name(records)).strip().lower()
    if not _NAME_RE.match(name):
        raise SandboxError(f"bad sandbox name {name!r} (a-z, 0-9, - and _, up to 32)")
    for r in records:
        if r.name == name and r.status not in ("missing",):
            raise SandboxError(f"a sandbox called {name!r} already exists ({r.status})",
                               next_step=f"awnode sandbox rm {name}  (or pick --name)")
    records = [r for r in records if r.name != name]
    image = (image or DEFAULT_IMAGE).strip()
    if not _IMAGE_RE.match(image):
        raise SandboxError(f"bad image reference {image!r}")
    engine = engine or detect_engine(runner)

    sid = "sb" + secrets.token_hex(3)
    ws = sandboxes_root() / sid / "work"
    ws.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # The box runs as uid 65534: its own /work must be writable by it. The parent
        # stays 0700, so no other local user can reach the world-writable folder.
        os.chmod(ws.parent, 0o700)
        os.chmod(ws, 0o777)
    rec = SandboxRecord(id=sid, name=name, engine=engine.label, image=image,
                        workspace=str(ws), created=_now().isoformat(timespec="seconds"),
                        network="bridge" if net else "none", ttl_hours=ttl_hours)

    if runner(engine.cmd("image", "inspect", image), timeout=60).returncode != 0:
        pull = runner(engine.cmd("pull", image), timeout=3600, stream=stream)
        if pull.returncode != 0:
            _rmtree_quiet(ws.parent)
            raise SandboxError(f"could not pull {image}: {pull.stderr.strip()[:300]}",
                               next_step="check the network, or pass --image <one you have>")
    if "@sha256:" not in image:
        d = runner(engine.cmd("image", "inspect", "--format",
                              "{{index .RepoDigests 0}}", image), timeout=60)
        if d.returncode == 0 and "@sha256:" in d.stdout:
            rec.image = d.stdout.strip().splitlines()[0]

    res = runner(run_argv(rec, engine, net=net), timeout=300)
    if res.returncode != 0:
        runner(engine.cmd("rm", "-f", rec.container), timeout=60)
        _rmtree_quiet(ws.parent)
        raise SandboxError(f"the engine refused to create the sandbox: "
                           f"{res.stderr.strip()[:400]}")
    try:
        # The kernel's view from inside, when the image has `cat`; inspect otherwise.
        st = runner(engine.cmd("exec", rec.container, "cat", "/proc/1/status"), timeout=60)
        problems = verify_confinement(_inspect(engine, rec.container, runner), net=net,
                                      proc_status=st.stdout if st.returncode == 0 else "")
    except (SandboxError, ValueError) as exc:
        problems = [f"could not read the container back: {exc}"]
    if problems:
        runner(engine.cmd("rm", "-f", rec.container), timeout=60)
        _rmtree_quiet(ws.parent)
        raise ConfinementError(problems)

    rec.status = "running"
    records.append(rec)
    save_records(records)
    return rec


def exec_in(ident: str, command: Sequence[str], *, runner: Optional[Runner] = None,
            stream: bool = True, timeout: Optional[float] = None) -> RunResult:
    """Run a command INSIDE the sandbox container (never on the host)."""
    runner = runner or default_runner
    if not command:
        raise SandboxError("nothing to run",
                           next_step="awnode sandbox run t1 -- python3 -c \"print(1)\"")
    records = load_records()
    rec = find(ident, records)
    reconcile([rec], runner)
    eng = engine_from_label(rec.engine)
    if rec.status == "expired":
        raise SandboxError(f"sandbox {rec.name} expired ({rec.ttl_hours}h)",
                           next_step=f"awnode sandbox rm {rec.name} --keep-work, "
                                     "then awnode sandbox new")
    if rec.status == "missing":
        raise SandboxError(f"sandbox {rec.name}'s container is gone",
                           next_step=f"awnode sandbox rm {rec.name} --keep-work")
    if rec.status == "engine_unreachable":
        raise SandboxError(f"engine {rec.engine} is not answering",
                           next_step="start it (on Windows: wsl -d awnix), then try again")
    if rec.status == "stopped":
        st = runner(eng.cmd("start", rec.container), timeout=120)
        if st.returncode != 0:
            raise SandboxError(f"could not start {rec.name}: {st.stderr.strip()[:300]}")
        rec.status = "running"
    _save_status(rec)
    argv = eng.cmd("exec", "--workdir", WORKDIR, rec.container, *command)
    return runner(argv, timeout=timeout, stream=stream)


def stop(ident: str, *, runner: Optional[Runner] = None) -> SandboxRecord:
    runner = runner or default_runner
    rec = find(ident)
    eng = engine_from_label(rec.engine)
    r = runner(eng.cmd("stop", "-t", "2", rec.container), timeout=120)
    if r.returncode != 0 and "no such" not in (r.stderr or "").lower():
        raise SandboxError(f"could not stop {rec.name}: {r.stderr.strip()[:300]}")
    rec.status = "stopped"
    _save_status(rec)
    return rec


def remove(ident: str, *, delete_workspace: bool = False,
           runner: Optional[Runner] = None) -> Dict:
    """Remove the container and the record; the workspace only if asked."""
    runner = runner or default_runner
    records = load_records()
    rec = find(ident, records)
    eng = engine_from_label(rec.engine)
    ws = Path(rec.workspace)
    if delete_workspace:
        # Files the container wrote belong to uid 65534; clear them from inside first.
        runner(eng.cmd("exec", rec.container, "sh", "-c",
                       "rm -rf /work/..?* /work/.[!.]* /work/*"), timeout=120)
    r = runner(eng.cmd("rm", "-f", rec.container), timeout=120)
    if r.returncode != 0 and "no such" not in (r.stderr or "").lower():
        raise SandboxError(f"could not remove {rec.name}: {r.stderr.strip()[:300]}")
    deleted = False
    if delete_workspace and _inside(ws, sandboxes_root()):
        _rmtree_quiet(ws.parent)
        deleted = not ws.exists()
    save_records([x for x in records if x.id != rec.id])
    return {"id": rec.id, "name": rec.name, "container_removed": True,
            "workspace": str(ws), "workspace_deleted": deleted}


def listing(runner: Optional[Runner] = None) -> Dict:
    runner = runner or default_runner
    records = load_records()
    orphans = reconcile(records, runner) if records else {}
    if records:
        save_records(records)
    return {"sandboxes": [r.to_dict() for r in records], "orphans": orphans}


def _save_status(rec: SandboxRecord) -> None:
    records = load_records()
    for i, r in enumerate(records):
        if r.id == rec.id:
            records[i] = rec
    save_records(records)


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rmtree_quiet(p: Path) -> None:
    if _inside(p, sandboxes_root()) and p.exists():
        shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# Journey + MCP opt-in
# ---------------------------------------------------------------------------

JOURNEY_BASE = "https://api.aitherium.com/api/bridge/genesis"
JOURNEY_PATH = "/api/v1/me/journey/facts"


def _auth_token() -> str:
    try:
        store = json.loads((aither_home() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    prof = (store.get("profiles") or {}).get(store.get("active_profile", ""), {})
    return str(prof.get("access_token") or "")


def post_journey_fact(rec: SandboxRecord, *, opener: Optional[Callable] = None) -> bool:
    """Best effort: tell the journey record 'own_sandbox' is proven. Never raises.

    Only when signed in. A 404 (journey API not deployed yet), a timeout or no
    network all just return False.
    """
    token = _auth_token()
    if not token:
        return False
    import urllib.request
    base = os.environ.get("AITHER_JOURNEY_URL", JOURNEY_BASE).rstrip("/")
    body = json.dumps({"rung": "own_sandbox", "source": "awnode",
                       "evidence": {"sandbox": rec.id, "engine": rec.engine,
                                    "image": rec.image, "network": rec.network,
                                    "where": rec.where}}).encode()
    req = urllib.request.Request(base + JOURNEY_PATH, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=3) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001 -- best effort by design
        return False


def mcp_allowed() -> bool:
    """MCP exec tools are opt-in: `awnode mcp --allow-sandbox` or config sandbox.allow_mcp."""
    if os.environ.get("AWNODE_MCP_ALLOW_SANDBOX", "").lower() in ("1", "true", "yes"):
        return True
    cfg = aither_home() / "config.yaml"
    if not cfg.is_file():
        return False
    try:
        import yaml
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 -- unreadable config = not allowed
        return False
    sb = data.get("sandbox") if isinstance(data, dict) else None
    return bool(isinstance(sb, dict) and sb.get("allow_mcp") is True)


def ready_line(rec: SandboxRecord) -> str:
    return (f"sandbox {rec.name} ready - next: awnode sandbox run {rec.name} "
            f"-- python3 -c \"print(1)\"")


__all__ = [
    "DEFAULT_IMAGE", "Engine", "RunResult", "SandboxRecord", "SandboxError",
    "NoEngine", "ConfinementError", "NotFound", "detect_engine", "create",
    "exec_in", "stop", "remove", "listing", "reconcile", "run_argv",
    "verify_confinement", "post_journey_fact", "mcp_allowed", "ready_line",
]
