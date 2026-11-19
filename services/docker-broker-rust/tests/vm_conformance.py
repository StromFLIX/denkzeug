#!/usr/bin/env python3
"""Run Docker conformance in a disposable local QEMU guest without a host socket.

For constrained Debian x86-64 development sandboxes. No root, KVM, host Docker,
provider keys or service configuration changes. Downloads are public toolchain
artifacts. The guest has no inbound network forwarding. Use --mode package only
when public npm egress is intended. All guest files live under --workdir.
"""
# Embedded guest scripts intentionally retain complete shell commands.
# Subprocesses execute fixed tooling or the explicitly selected, locally built test binaries.
# ruff: noqa: E501, S603, S607

import argparse
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[3]
CRATE = REPO / "services/docker-broker-rust"


def run(args, **kw):
    return subprocess.run(list(map(str, args)), check=True, **kw)


def download(url, path):
    if path.exists():
        return
    if not url.startswith("https://"):
        raise ValueError("toolchain downloads require HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "Denkzeug-local-conformance"})  # noqa: S310 - fixed HTTPS toolchain origins.
    with (
        urllib.request.urlopen(request, timeout=90) as incoming,  # noqa: S310 - fixed HTTPS toolchain origins.
        path.with_suffix(".partial").open("wb") as out,
    ):  # noqa: S310 - fixed HTTPS toolchain origins.
        shutil.copyfileobj(incoming, out)
    path.with_suffix(".partial").replace(path)


def put(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    shutil.copy2(src, dst)


def binary(src, root):
    src = pathlib.Path(src).resolve()
    put(src, root / str(src).lstrip("/"))
    result = subprocess.run(["ldd", str(src)], capture_output=True, text=True)
    for path in set(re.findall(r"(/[\w/.+\-]+\.so(?:\.\d+)*)", result.stdout)):
        put(path, root / path.lstrip("/"))


def prepare_tools(work):
    apt = work / "apt"
    qemu = work / "qemu"
    for sub in ["lists/partial", "archives/partial"]:
        (apt / sub).mkdir(parents=True, exist_ok=True)
    (apt / "sources.list").write_text(
        "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main\n"
    )
    opts = [
        "-o",
        f"Dir::Etc::sourcelist={apt}/sources.list",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        f"Dir::State::Lists={apt}/lists",
        "-o",
        f"Dir::Cache::Archives={apt}/archives",
        "-o",
        f"Dir::Cache::pkgcache={apt}/pkgcache.bin",
        "-o",
        f"Dir::Cache::srcpkgcache={apt}/srcpkgcache.bin",
        "-o",
        "Debug::NoLocking=true",
    ]
    if not (qemu / "usr/bin/qemu-system-x86_64").exists():
        run(["apt-get", *opts, "update"])
        run(
            [
                "apt-get",
                *opts,
                "-y",
                "--download-only",
                "--no-install-recommends",
                "install",
                "qemu-system-x86",
                "seabios",
                "busybox-static",
                "iptables",
            ]
        )
        run(
            [
                "apt-get",
                *opts,
                "download",
                "libfuse3-3",
                "libnl-3-200",
                "libmnl0",
                "libnftnl11",
                "libxtables12",
                "libnftables1",
            ],
            cwd=apt / "archives",
        )
        for deb in (apt / "archives").glob("*.deb"):
            run(["dpkg-deb", "-x", deb, qemu])
    kernel = work / "kernel"
    if not (kernel / "boot/vmlinuz-virt").exists():
        kernel.mkdir(exist_ok=True)
        download(
            "https://dl-cdn.alpinelinux.org/alpine/v3.22/main/x86_64/linux-virt-6.12.118-r0.apk",
            work / "kernel.apk",
        )
        run(["tar", "-izxf", work / "kernel.apk", "-C", kernel], stderr=subprocess.DEVNULL)
    if not (work / "docker/dockerd").exists():
        download(
            "https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz", work / "docker.tgz"
        )
        run(["tar", "-xzf", work / "docker.tgz", "-C", work])
    return qemu, kernel


def compile_tests():
    output = run(
        [
            "cargo",
            "test",
            "--locked",
            "--manifest-path",
            CRATE / "Cargo.toml",
            "--no-run",
            "--test",
            "docker_inventory",
            "--test",
            "docker_registry",
            "--test",
            "docker_package",
            "--message-format=json",
        ],
        capture_output=True,
        text=True,
    )
    tests = {}
    for line in output.stdout.splitlines():
        item = json.loads(line)
        if item.get("executable"):
            tests[item["target"]["name"]] = pathlib.Path(item["executable"])
    return tests


def image_root(work, qemu, kind):
    tar = work / f"{kind}.tar"
    stamp = work / f"{kind}.stamp"
    digest = hashlib.sha256(pathlib.Path(__file__).read_bytes())
    for directory in [REPO / "images/pi-base", CRATE / "tests"]:
        for file in sorted(directory.rglob("*")):
            if file.is_file() and file.suffix in {".mjs", ".cjs", ".ts", ".json", ".py"}:
                digest.update(file.read_bytes())
    version = digest.hexdigest()
    if tar.exists() and stamp.exists() and stamp.read_text() == version:
        return tar
    root = work / f"root-{kind}"
    if root.exists():
        shutil.rmtree(root)
    for sub in ["bin", "tmp", "etc", "workspace", "root", "fixture", "usr/local/bin"]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "tmp").chmod(0o1777)
    put(qemu / "bin/busybox", root / "bin/busybox")
    for name in ["sh", "sleep", "cat", "touch", "mkdir", "ls", "cp", "id", "false"]:
        if not (root / "bin" / name).is_symlink():
            (root / "bin" / name).symlink_to("busybox")
    (root / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\napp:x:10001:10001:app:/workspace:/bin/sh\n"
    )
    (root / "etc/group").write_text("root:x:0:\napp:x:10001:\n")
    if kind in {"node", "pi", "package"}:
        binary(shutil.which("node"), root)
        binary("/usr/bin/env", root)
        put(CRATE / "tests/docker_chat_fixture.cjs", root / "agent-fixture.cjs")
    if kind == "node":
        binary("/usr/bin/git", root)
        put(REPO / "images/pi-base/project-git.mjs", root / "usr/local/lib/nautionette/project-git.mjs")
        put(REPO / "images/pi-base/brand.json", root / "usr/local/lib/nautionette/brand.json")
    if kind == "package":
        npm = pathlib.Path(shutil.which("npm")).resolve().parent.parent
        shutil.copytree(npm, root / str(npm).lstrip("/"), symlinks=True, dirs_exist_ok=True)
        (root / "usr/local/bin/npm").symlink_to(str(npm / "bin/npm-cli.js"))
        put(
            REPO / "images/pi-base/package-install.mjs",
            root / "usr/local/lib/nautionette/package-install.mjs",
        )
    if kind == "pi":
        binary("/bin/bash", root)
        pi = pathlib.Path(shutil.which("pi")).resolve().parents[2]
        # The sandbox CLI is at dist/bundle/cli.js; copy the whole pinned npm package.
        while pi.name != "pi-coding-agent":
            pi = pi.parent
        shutil.copytree(pi, root / str(pi).lstrip("/"), symlinks=True, dirs_exist_ok=True)
        (root / "usr/local/bin/pi").symlink_to(str(pathlib.Path(shutil.which("pi")).resolve()))
        for file in (REPO / "images/pi-base").glob("*.mjs"):
            put(file, root / "usr/local/lib/nautionette" / file.name)
        put(REPO / "images/pi-base/brand.json", root / "usr/local/lib/nautionette/brand.json")
        for name in ["chat-model.mjs", "chat-provider.ts"]:
            put(REPO / "tests/agent/fixtures" / name, root / "fixture" / name)
        put(CRATE / "tests/docker_pi_steer.cjs", root / "fixture/steer.cjs")
    if kind == "artcraft":
        exe = pathlib.Path(sys.executable).resolve()
        prefix = exe.parent.parent
        binary(exe, root)
        stdlib = next((prefix / "lib").glob("python3.[0-9][0-9]"))
        shutil.copytree(
            stdlib,
            root / str(stdlib).lstrip("/"),
            symlinks=True,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "site-packages", "__pycache__", "test", "tkinter", "idlelib", "turtledemo"
            ),
        )
        for lib in (stdlib / "lib-dynload").glob("*.so"):
            binary(lib, root)
        (root / "usr/local/bin/python3").symlink_to(str(exe))
        for name in ["install.py", "catalog.json"]:
            put(REPO / "images/pi-base/artcraft" / name, root / "usr/local/lib/nautionette/artcraft" / name)
        for path in set(
            re.findall(
                r"(/[\w/.+\-]+\.so(?:\.\d+)*)",
                subprocess.check_output(["ldd", str(work / "grid-artifact/bin/gridcraft-cli")], text=True),
            )
        ):
            put(path, root / path.lstrip("/"))
    run(["tar", "--owner=0", "--group=0", "-C", root, "-cf", tar, "."])
    stamp.write_text(version)
    return tar


def initramfs(work, kernel, busy):
    root = work / "initrd"
    (root / "dev").mkdir(parents=True, exist_ok=True)
    put(busy, root / "bin/busybox")
    (root / "init").write_text("""#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
set -eu
/bin/busybox modprobe sd_mod
/bin/busybox modprobe crc32c_generic
/bin/busybox modprobe ext4
for n in 1 2 3 4 5; do [ -b /dev/sda ] && break; /bin/busybox sleep 1; done
/bin/busybox mkdir -p /newroot
/bin/busybox mount -t ext4 -o rw /dev/sda /newroot
/bin/busybox mount --move /dev /newroot/dev
exec /bin/busybox switch_root /newroot /stage2
""")
    (root / "init").chmod(0o755)
    modules = next((kernel / "lib/modules").iterdir())
    needed = {
        "sd_mod.ko.gz",
        "ext4.ko.gz",
        "crc32c_generic.ko.gz",
        "libcrc32c.ko.gz",
        "crc32_generic.ko.gz",
        "crc16.ko.gz",
        "mbcache.ko.gz",
        "jbd2.ko.gz",
    }
    for file in modules.rglob("*"):
        if file.is_file() and (file.name.startswith("modules.") or file.name in needed):
            put(file, root / "lib/modules" / modules.name / file.relative_to(modules))
    target = work / "initrd.gz"
    with gzip.open(target, "wb", compresslevel=1) as out:

        def add(i, name, mode, size, data=None, major=0, minor=0):
            name = os.fsencode(name) + b"\0"
            header = b"070701" + b"".join(
                f"{v:08x}".encode() for v in (i, mode, 0, 0, 1, 0, size, 0, 0, major, minor, len(name), 0)
            )
            out.write(header + name + b"\0" * (-len(header + name) % 4))
            if data is not None:
                if isinstance(data, bytes):
                    out.write(data)
                else:
                    with data.open("rb") as src:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            out.write(b"\0" * (-size % 4))

        for i, file in enumerate(sorted(root.rglob("*")), 2):
            mode = file.lstat().st_mode
            data = (
                os.fsencode(os.readlink(file)) if stat.S_ISLNK(mode) else file if stat.S_ISREG(mode) else None
            )
            size = len(data) if isinstance(data, bytes) else file.stat().st_size if data else 0
            add(i, file.relative_to(root).as_posix(), mode, size, data)
        add(i + 1, "dev/console", stat.S_IFCHR | 0o600, 0, major=5, minor=1)
        add(i + 2, "TRAILER!!!", 0, 0)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=pathlib.Path)
    parser.add_argument(
        "--mode", choices=["wire", "pi", "artcraft", "registry", "package", "scratch"], required=True
    )
    parser.add_argument("--memory", default=544, type=int)
    args = parser.parse_args()
    work = args.workdir.resolve()
    if work == REPO or REPO in work.parents:
        parser.error("use a disposable scratch directory outside the repository")
    marker = work / ".denkzeug-broker-fixture"
    if work.exists() and any(work.iterdir()) and not marker.exists():
        parser.error("refusing to replace an unrecognized, nonempty fixture directory")
    work.mkdir(parents=True, exist_ok=True)
    marker.write_text("Docker broker disposable VM fixture\n")
    if os.uname().machine != "x86_64":
        parser.error("this fixture bootstrap currently requires an x86-64 host")
    qemu, kernel = prepare_tools(work)
    tests = compile_tests()
    guest = work / "guest"
    if guest.exists():
        shutil.rmtree(guest)
    for name in ["work", "etc", "bin", "usr/bin", "dev", "proc", "sys", "run", "tmp", "var", "root"]:
        (guest / name).mkdir(parents=True, exist_ok=True)
    (guest / "tmp").chmod(0o1777)
    put(qemu / "bin/busybox", guest / "bin/busybox")
    binary("/usr/bin/git", guest)
    for name, file in tests.items():
        binary(file, guest)
        shutil.move(guest / str(file).lstrip("/"), guest / "work" / name)
    shutil.copytree(work / "docker", guest / "docker-tools", dirs_exist_ok=True)
    shutil.copytree(
        kernel / "lib/modules",
        guest / "lib/modules",
        dirs_exist_ok=True,
        symlinks=True,
        ignore=shutil.ignore_patterns("build", "source"),
    )
    for source in [qemu / "usr/lib/x86_64-linux-gnu", qemu / "lib/x86_64-linux-gnu"]:
        if source.is_dir():
            shutil.copytree(source, guest / "usr/lib/x86_64-linux-gnu", symlinks=True, dirs_exist_ok=True)
    for source in [qemu / "usr/sbin", qemu / "sbin"]:
        if source.is_dir():
            shutil.copytree(source, guest / "usr/sbin", symlinks=True, dirs_exist_ok=True)
    for name in ["iptables", "ip6tables"]:
        link = guest / "usr/sbin" / name
        if link.is_symlink():
            link.unlink()
        link.symlink_to("xtables-legacy-multi")
    (guest / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
    (guest / "etc/group").write_text("root:x:0:\n")
    (guest / "etc/hosts").write_text("127.0.0.1 localhost\n")
    (guest / "etc/resolv.conf").write_text("nameserver 10.0.2.3\n")
    boot = """#!/bin/busybox sh
export PATH=/docker-tools:/bin:/usr/sbin:/usr/bin
/bin/busybox --install -s /bin
set -eu
trap 'rc=$?; echo "DENKZEUG_VM_RESULT=$rc"; poweroff -f; sleep 5' EXIT
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /sys/fs/cgroup /dev/pts /dev/shm /run /tmp /var/lib/docker
mount -t cgroup2 none /sys/fs/cgroup
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=1777 tmpfs /dev/shm
modprobe overlay
ip link set lo up
"""
    network = args.mode == "package"
    if network:
        boot += """modprobe e1000
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
ip route add default via 10.0.2.2
modprobe br_netfilter
modprobe nf_nat
modprobe ip_tables
modprobe iptable_nat
modprobe iptable_filter
modprobe ip6table_filter
modprobe ip6table_nat
modprobe xt_conntrack
modprobe xt_MASQUERADE
mkdir -p /sbin
ln -sf /bin/modprobe /sbin/modprobe
echo 1 >/proc/sys/net/ipv4/ip_forward
"""
    flags = (
        ""
        if network
        else "--iptables=false --ip6tables=false --bridge=none --ip-forward=false --ip-masq=false"
    )
    boot += f"""dockerd --host=unix:///var/run/docker.sock --storage-driver=overlay2 {flags} >/tmp/dockerd.log 2>&1 &
for n in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
if ! docker info; then tail -80 /tmp/dockerd.log; exit 1; fi
export BROKER_DOCKER_SOCKET=/var/run/docker.sock
"""
    if args.mode == "scratch":
        broker = pathlib.Path(os.environ["BROKER_STATIC_BINARY"]).resolve()
        inputs = [path for path in (CRATE / "src").glob("*.rs") if path.name != "parity_tests.rs"] + [
            CRATE / "Cargo.toml",
            CRATE / "Cargo.lock",
        ]
        if broker.stat().st_mtime < max(path.stat().st_mtime for path in inputs):
            parser.error("rebuild BROKER_STATIC_BINARY from the current source before the scratch check")
        if (
            "statically linked"
            not in subprocess.run(["ldd", str(broker)], capture_output=True, text=True).stdout
        ):
            parser.error("BROKER_STATIC_BINARY must be statically linked")
        context = guest / "work/scratch-context"
        put(broker, context / "broker")
        shutil.copytree(
            REPO / "images",
            context / "images",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "target"),
        )
        for path, mode in [
            ("root-owned/tmp", 0o1777),
            ("shared/projects", 0o755),
            ("shared/chat-sessions", 0o700),
        ]:
            target = context / "runtime" / path
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(mode)
        dockerfile = (CRATE / "Dockerfile.spike").read_text()
        runtime = "FROM scratch AS candidate" + dockerfile.split("FROM scratch AS candidate", 1)[1]
        (context / "Dockerfile").write_text(
            "FROM scratch AS compile\nCOPY broker /broker\nCOPY runtime /runtime\n\n" + runtime
        )
        boot += """cd /work/scratch-context
DOCKER_BUILDKIT=0 docker build --network=none --target smoke -t broker-scratch:smoke .
docker run --rm --network=none broker-scratch:smoke >/tmp/fingerprints.json
cmp /tmp/fingerprints.json images/source-fingerprints.json
if docker run --rm --network=none --entrypoint /bin/sh broker-scratch:smoke -c true; then echo 'Scratch unexpectedly has a shell'; exit 1; fi
DOCKER_BUILDKIT=0 docker build --network=none --target candidate -t broker-scratch:candidate .
docker volume create scratch-sessions
docker volume create scratch-projects
docker run -d --name scratch-broker --network=none -e INTERNAL_TOKEN=synthetic-local-token -v scratch-sessions:/chat-sessions -v scratch-projects:/projects broker-scratch:candidate
session=$(docker volume inspect --format '{{.Mountpoint}}' scratch-sessions)
project=$(docker volume inspect --format '{{.Mountpoint}}' scratch-projects)
[ "$(stat -c '%u:%g:%a' "$session")" = '10001:10001:700' ]
[ "$(stat -c '%u:%g:%a' "$project")" = '10001:10001:755' ]
sleep 1
if docker exec scratch-broker /broker healthcheck; then echo 'No Docker must not be reported ready'; exit 1; fi
[ "$(docker inspect --format '{{.State.Running}}' scratch-broker)" = true ]
docker image inspect --format 'SCRATCH_IMAGE_BYTES={{.Size}}' broker-scratch:candidate
docker rm -f scratch-broker
docker volume rm scratch-sessions scratch-projects
echo 'test docker_scratch_runtime_is_static_shell_free_and_fails_closed ... ok'
"""
    elif args.mode == "registry":
        boot += "/work/docker_registry --ignored --nocapture\n"
    elif args.mode == "wire":
        for kind, change in [
            ("busybox", 'CMD ["/bin/busybox", "true"]'),
            ("node", 'CMD ["node", "/agent-fixture.cjs"]'),
        ]:
            put(image_root(work, qemu, kind), guest / "work" / f"{kind}.tar")
            boot += f"docker import --change '{change}' /work/{kind}.tar {kind}:fixture\n"
        boot += "export BROKER_FIXTURE_IMAGE=busybox:fixture BROKER_NODE_FIXTURE_IMAGE=node:fixture\n/work/docker_inventory --ignored --nocapture --test-threads=1 --skip docker_real_pi --skip docker_real_artcraft\n"
    elif args.mode == "pi":
        assert subprocess.check_output(["pi", "--version"], text=True).strip() == "1.5.2"
        put(image_root(work, qemu, "pi"), guest / "work/pi.tar")
        boot += """docker import --change 'WORKDIR /workspace' --change 'ENTRYPOINT ["node", "/usr/local/lib/nautionette/agent-run.mjs"]' /work/pi.tar pi:native
printf '%s\n' 'FROM pi:native' 'ENTRYPOINT ["node", "/fixture/steer.cjs"]' | DOCKER_BUILDKIT=0 docker build --network=none -t pi:steer -
export BROKER_PI_FIXTURE_IMAGE=pi:native BROKER_PI_STEER_FIXTURE_IMAGE=pi:steer
/work/docker_inventory docker_real_pi --ignored --nocapture --test-threads=1
"""
    elif args.mode == "artcraft":
        if not (work / "grid-artifact/manifest.json").exists():
            run([sys.executable, CRATE / "tests/prepare_gridcraft_fixture.py", work / "grid-artifact"])
        put(image_root(work, qemu, "artcraft"), guest / "work/artcraft.tar")
        shutil.copytree(work / "grid-artifact", guest / "work/grid-artifact", dirs_exist_ok=True)
        boot += """docker import --change 'ENV PATH=/usr/local/bin:/usr/bin:/bin' --change 'CMD ["/bin/sh", "-c", ":"]' /work/artcraft.tar artcraft:fixture
export BROKER_ARTCRAFT_ARTIFACT_PATH=/work/grid-artifact BROKER_ARTCRAFT_VERIFY_IMAGE=artcraft:fixture
/work/docker_inventory docker_real_artcraft --ignored --nocapture --test-threads=1
"""
    else:
        put(image_root(work, qemu, "package"), guest / "work/package.tar")
        boot += """docker import --change 'ENV PATH=/usr/local/bin:/usr/bin:/bin' --change 'CMD ["/bin/sh", "-c", ":"]' /work/package.tar package:fixture
export BROKER_PACKAGE_FIXTURE_IMAGE=package:fixture NAUTIONETTE_PACKAGE_TEST_SOURCE=npm:@oscaner-skills/kairos@1.7.0
/work/docker_package --ignored --nocapture
"""
    (guest / "stage2").write_text(boot)
    (guest / "stage2").chmod(0o755)
    disk = work / "guest.ext4"
    run(["truncate", "-s", "0", disk])
    run(["truncate", "-s", "7G", disk])
    run(
        [
            "/usr/sbin/mke2fs",
            "-q",
            "-F",
            "-t",
            "ext4",
            "-O",
            "^metadata_csum,^metadata_csum_seed",
            "-d",
            guest,
            disk,
        ]
    )
    initrd = initramfs(work, kernel, qemu / "bin/busybox")
    for tree in [work, pathlib.Path(os.environ.get("CARGO_TARGET_DIR", CRATE / "target"))]:
        for file in tree.rglob("*"):
            if file.is_symlink() or not file.is_file():
                continue
            try:
                with file.open("rb") as stream:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
    env = dict(os.environ, LD_LIBRARY_PATH=f"{qemu}/usr/lib/x86_64-linux-gnu:{qemu}/lib/x86_64-linux-gnu")
    net = ["-nic", "user,model=e1000"] if network else ["-nic", "none"]
    command = [
        qemu / "usr/bin/qemu-system-x86_64",
        "-L",
        qemu / "usr/share/qemu",
        "-bios",
        qemu / "usr/share/seabios/bios-256k.bin",
        "-machine",
        "pc",
        "-accel",
        "tcg,thread=single,tb-size=96",
        "-cpu",
        "qemu64",
        "-smp",
        "1",
        "-m",
        str(args.memory),
        *net,
        "-display",
        "none",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-no-reboot",
        "-drive",
        f"file={disk},format=raw,if=ide,cache=none",
        "-kernel",
        kernel / "boot/vmlinuz-virt",
        "-initrd",
        initrd,
        "-append",
        "console=ttyS0 quiet rdinit=/init panic=3 nokaslr ftrace_enabled=0",
    ]
    log = work / f"{args.mode}.log"
    with log.open("w") as output:
        result = subprocess.run(
            list(map(str, command)), env=env, stdout=output, stderr=subprocess.STDOUT, timeout=700
        )
    text = log.read_text()
    print(text)
    if result.returncode or "DENKZEUG_VM_RESULT=0" not in text:
        raise SystemExit(f"isolated Docker conformance failed; see {log}")


if __name__ == "__main__":
    main()
