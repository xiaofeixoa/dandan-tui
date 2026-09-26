# Kernel Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add system kernel management, GRUB confirmation, and a separate VPN-specific kernel category without changing existing TCP tuning behavior.

**Architecture:** kernel_manager.py contains pure support-matrix, package-plan, Mainline listing, and GRUB parser code. tui.py owns terminal interaction and dispatch. The MIT source builder is a complete local subtree, with no remote helper loading.

**Tech Stack:** Python standard library, unittest, Bash, APT/dpkg, GRUB.

---

### Task 1: Kernel Facts And Distribution Plans

**Files:**
- Create: kernel_manager.py
- Create: tests/test_kernel_manager.py

- [ ] **Step 1: Write failing support-matrix tests**

    from kernel_manager import KernelIdentity, supported_distribution

    def test_supported_release_matrix():
        assert supported_distribution(KernelIdentity("debian", "11", "bullseye", "amd64"))
        assert supported_distribution(KernelIdentity("debian", "13", "trixie", "arm64"))
        assert supported_distribution(KernelIdentity("ubuntu", "22.04", "jammy", "amd64"))
        assert supported_distribution(KernelIdentity("ubuntu", "26.04", "resolute", "arm64"))
        assert not supported_distribution(KernelIdentity("debian", "10", "buster", "amd64"))

- [ ] **Step 2: Verify red**

Run: python3 -m unittest -v tests.test_kernel_manager

Expected: ModuleNotFoundError for kernel_manager.

- [ ] **Step 3: Implement the minimal model**

    @dataclass(frozen=True)
    class KernelIdentity:
        distro_id: str
        version_id: str
        codename: str
        architecture: str

    def supported_distribution(identity: KernelIdentity) -> bool:
        return identity.architecture in {"amd64", "arm64"} and (
            identity.distro_id, identity.version_id
        ) in {("debian", "11"), ("debian", "12"), ("debian", "13"),
              ("ubuntu", "22.04"), ("ubuntu", "24.04"), ("ubuntu", "26.04")}

- [ ] **Step 4: Write failing package-plan and Mainline rejection tests**

    def test_mainline_requires_successful_complete_amd64_build():
        page = "Test amd64 missing\nTest arm64 missing\nCHECKSUMS\nCHECKSUMS.gpg"
        assert mainline_plan("v7.1.8", page, "amd64") is None
        assert mainline_plan("v7.1.8", page, "arm64") is None

    def test_apt_plan_requires_visible_packages():
        identity = KernelIdentity("debian", "12", "bookworm", "amd64")
        assert recommended_apt_plan(identity, {}) is None
        assert recommended_apt_plan(identity, {"linux-image-amd64": "6.1.0-35"})

- [ ] **Step 5: Implement immutable plans, run green, and commit**

    @dataclass(frozen=True)
    class PackagePlan:
        label: str
        packages: tuple[str, ...]
        source: str

    def mainline_plan(version: str, page: str, architecture: str) -> PackagePlan | None:
        required = ("CHECKSUMS", "CHECKSUMS.gpg", "linux-headers-", "linux-image")
        if architecture != "amd64" or "Test amd64 succeeded" not in page:
            return None
        return PackagePlan(f"Ubuntu Mainline {version}", (), "kernel.ubuntu.com") if all(
            item in page for item in required
        ) else None

Run: python3 -m unittest -v tests.test_kernel_manager

    git add kernel_manager.py tests/test_kernel_manager.py
    git commit -m "feat: add kernel planning primitives"

### Task 2: System Kernel Maintenance Menu

**Files:**
- Modify: kernel_manager.py
- Modify: tui.py
- Modify: scripts.json
- Modify: tests/test_kernel_manager.py
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing safety and registration tests**

    def test_container_install_is_blocked():
        facts = KernelFacts(identity=identity, virtualization="docker", bootloader="none")
        assert facts.installation_block_reason() == "容器共享宿主机内核，不能在容器内安装或切换内核。"

    def test_system_kernel_action_is_root_only(self):
        action = self.actions["system_kernel_maintenance"]
        self.assertEqual(action["category"], "kernel_manage")
        self.assertTrue(action["needs_root"])

- [ ] **Step 2: Verify red**

Run: python3 -m unittest -v tests.test_kernel_manager tests.test_smoke

Expected: missing KernelFacts and missing system_kernel_maintenance.

- [ ] **Step 3: Implement read-only facts and interaction**

    def collect_kernel_facts(run: CommandRunner = default_run) -> KernelFacts:
        identity = read_kernel_identity(run)
        return KernelFacts(
            identity=identity,
            running_kernel=run(("uname", "-r")).strip(),
            virtualization=run(("systemd-detect-virt",)).strip() or "none",
            boot_images=tuple(list_boot_images()),
            bootloader=detect_bootloader(),
        )

    def system_kernel_maintenance(self, log: Path) -> int:
        facts = kernel_manager.collect_kernel_facts()
        print(kernel_manager.format_kernel_report(facts))
        if reason := facts.installation_block_reason():
            print(reason)
            return 1

The install path prints a fixed plan, requires an operation-specific confirmation, uses only apt-get install on advertised packages, then refreshes initramfs and GRUB. It never runs apt-get upgrade, reboots, or purges packages.

- [ ] **Step 4: Run green and commit**

Run: python3 -m unittest -v tests.test_kernel_manager tests.test_smoke

    git add kernel_manager.py tui.py scripts.json tests/test_kernel_manager.py tests/test_smoke.py
    git commit -m "feat: add system kernel maintenance"

### Task 3: VPN Kernel Separation And Boot Maintenance

**Files:**
- Modify: kernel_manager.py
- Modify: tui.py
- Modify: scripts.json
- Modify: tests/test_kernel_manager.py
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing menu migration and GRUB parser tests**

    def test_kernel_categories_separate_system_and_vpn_actions(self):
        categories = [item["id"] for item in self.config["categories"]]
        self.assertEqual(categories[categories.index("tcp_tuning") + 1:categories.index("nodeseek")],
                         ["kernel_manage", "vpn_kernel"])
        self.assertEqual(self.actions["grub_manage"]["category"], "kernel_manage")
        self.assertEqual(self.actions["tcp_fsc_1"]["category"], "vpn_kernel")
        self.assertEqual(self.actions["tcp_bbr_fq"]["category"], "tcp_tuning")

    def test_grub_parser_returns_full_submenu_path():
        assert grub_menu_entries("submenu 'Advanced' { menuentry 'Linux 6.12' {} }") == (
            "Advanced>Linux 6.12",)

- [ ] **Step 2: Verify red**

Run: python3 -m unittest -v tests.test_kernel_manager tests.test_smoke

Expected: category and parser assertions fail.

- [ ] **Step 3: Move approved actions and implement full-entry GRUB selection**

    def resolve_grub_entry(entries: tuple[str, ...], selected: str) -> str | None:
        return selected if selected in entries else None

Add kernel_manage and vpn_kernel categories after tcp_tuning. Move only kernel_manage, grub_manage, tcp_fsc_0, tcp_fsc_1, tcp_fsc_2, tcp_fsc_3, tcp_fsc_5, tcp_fsc_8, tcp_fsc_9, tcp_fsc_19, tcp_fsc_20, and tcp_brutal. Preserve IDs. Keep DD, IP checks, TCP Brutal, sysctl profiles, and tcpfit in tcp_tuning.

Expand GRUB operations to display boot images, full menu entries, grub-editenv list, refresh state, backup/restore, next boot, permanent default, and post-reboot uname -r confirmation. Non-GRUB systems remain read-only.

- [ ] **Step 4: Run green and commit**

Run: python3 -m unittest -v tests.test_kernel_manager tests.test_smoke

    git add kernel_manager.py tui.py scripts.json tests/test_kernel_manager.py tests/test_smoke.py
    git commit -m "feat: separate vpn kernels and boot maintenance"

### Task 4: Vendor The Hardened MIT Source Builder

**Files:**
- Create: scripts/kernel-installer/kernel_installer.sh
- Create: scripts/kernel-installer/src/slib.sh
- Create: scripts/kernel-installer/LICENSE
- Create: scripts/kernel-installer/UPSTREAM.md
- Modify: tui.py
- Modify: launch.sh
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing vendoring tests**

    def test_kernel_source_builder_is_local_and_hardened(self):
        root = ROOT / "scripts/kernel-installer"
        self.assertTrue((root / "kernel_installer.sh").is_file())
        self.assertTrue((root / "src/slib.sh").is_file())
        self.assertIn("MIT License", (root / "LICENSE").read_text(encoding="utf-8"))
        source = (root / "kernel_installer.sh").read_text(encoding="utf-8")
        self.assertNotIn("source <(curl", source)
        self.assertNotIn("--no-check-certificate", source)

- [ ] **Step 2: Verify red**

Run: python3 -m unittest -v tests.test_smoke

Expected: the vendored directory assertion fails.

- [ ] **Step 3: Vendor only required upstream files and record provenance**

Import kernel_installer.sh, src/slib.sh, and LICENSE from one immutable tmiland/kernel-installer commit. Write UPSTREAM.md with source URL, commit, date, SHA-256, license, and every local delta. Do not import upstream logs/images.

- [ ] **Step 4: Harden source build and launcher**

Require the local helper, disable self-update and remote helper fallback, remove insecure certificate bypass, make verified tarballs default, and block kexec/uninstall through the TUI adapter. Add nested download/chmod/move entries in launch.sh.

- [ ] **Step 5: Run green and commit**

Run: bash -n scripts/kernel-installer/kernel_installer.sh && python3 -m unittest -v tests.test_smoke && ./run.sh --check

    git add scripts/kernel-installer tui.py launch.sh tests/test_smoke.py
    git commit -m "feat: vendor hardened kernel source builder"

### Task 5: Document And Verify

**Files:**
- Modify: README.md
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing launcher-list assertion**

    def test_kernel_actions_are_visible_in_launcher_list(self):
        result = subprocess.run(["./run.sh", "--list"], cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertIn("system_kernel_maintenance", result.stdout)

- [ ] **Step 2: Verify red, document the three routes, and run checks**

Document official APT, Mainline amd64-only, and source-build paths. State no route auto-reboots/purges and reboot plus uname -r proves final activation.

Run:

    python3 -m unittest discover -s tests -v
    ./run.sh --check
    ./run.sh --list
    bash -n scripts/kernel-installer/kernel_installer.sh
    git diff --check

- [ ] **Step 3: Run normal and 80x24 pseudo-terminal navigation, then commit**

    git add README.md tests/test_smoke.py
    git commit -m "docs: document kernel management workflow"
