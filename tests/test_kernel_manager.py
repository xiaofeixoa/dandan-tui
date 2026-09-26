import unittest

from kernel_manager import (
    KernelFacts,
    KernelIdentity,
    backports_apt_arguments,
    debian_backports_source,
    mainline_package_plan,
    mainline_sha256sums,
    parse_grub_menu_entries,
    recommended_apt_plan,
    resolve_grub_entry,
    supported_distribution,
)


class KernelIdentityTests(unittest.TestCase):
    def test_supports_requested_debian_and_ubuntu_versions_on_both_architectures(self):
        supported = (
            ("debian", "11", "bullseye", "amd64"),
            ("debian", "12", "bookworm", "arm64"),
            ("debian", "13", "trixie", "amd64"),
            ("ubuntu", "22.04", "jammy", "arm64"),
            ("ubuntu", "24.04", "noble", "amd64"),
            ("ubuntu", "26.04", "resolute", "arm64"),
        )
        for distro_id, version_id, codename, architecture in supported:
            with self.subTest(distro_id=distro_id, version_id=version_id, architecture=architecture):
                self.assertTrue(
                    supported_distribution(KernelIdentity(distro_id, version_id, codename, architecture))
                )

    def test_rejects_unsupported_release_and_architecture(self):
        self.assertFalse(supported_distribution(KernelIdentity("debian", "10", "buster", "amd64")))
        self.assertFalse(supported_distribution(KernelIdentity("ubuntu", "24.04", "noble", "armhf")))


class AptPlanTests(unittest.TestCase):
    def test_debian_stock_plan_requires_both_image_and_headers_candidates(self):
        identity = KernelIdentity("debian", "12", "bookworm", "amd64")
        self.assertIsNone(recommended_apt_plan(identity, {"linux-image-amd64": "6.1.0-35"}))

        plan = recommended_apt_plan(
            identity,
            {"linux-image-amd64": "6.1.0-35", "linux-headers-amd64": "6.1.0-35"},
        )

        self.assertEqual(plan.label, "Debian 12 官方稳定内核")
        self.assertEqual(plan.packages, ("linux-image-amd64", "linux-headers-amd64"))
        self.assertEqual(plan.source, "configured-apt")

    def test_ubuntu_hwe_plan_is_only_offered_when_apt_exposes_it(self):
        identity = KernelIdentity("ubuntu", "22.04", "jammy", "amd64")
        self.assertIsNone(recommended_apt_plan(identity, {}))

        plan = recommended_apt_plan(
            identity,
            {
                "linux-generic-hwe-22.04": "6.8.0.85.86",
                "linux-headers-generic-hwe-22.04": "6.8.0.85.86",
            },
            preferred_track="hwe",
        )

        self.assertEqual(plan.label, "Ubuntu 22.04 HWE 内核")
        self.assertEqual(
            plan.packages,
            ("linux-generic-hwe-22.04", "linux-headers-generic-hwe-22.04"),
        )

    def test_debian_backports_source_is_limited_to_supported_debian_releases(self):
        identity = KernelIdentity("debian", "12", "bookworm", "arm64")

        self.assertEqual(
            debian_backports_source(identity),
            "deb http://deb.debian.org/debian bookworm-backports main",
        )
        self.assertIsNone(debian_backports_source(KernelIdentity("ubuntu", "24.04", "noble", "amd64")))
        self.assertIsNone(debian_backports_source(KernelIdentity("debian", "10", "buster", "amd64")))

    def test_backports_install_targets_its_suite_explicitly(self):
        identity = KernelIdentity("debian", "12", "bookworm", "amd64")

        self.assertEqual(
            backports_apt_arguments(identity, ("linux-image-amd64", "linux-headers-amd64")),
            ("apt-get", "install", "-y", "-t", "bookworm-backports", "linux-image-amd64", "linux-headers-amd64"),
        )
        self.assertIsNone(backports_apt_arguments(KernelIdentity("ubuntu", "24.04", "noble", "amd64"), ("linux-generic",)))


class MainlinePlanTests(unittest.TestCase):
    COMPLETE_AMD64_PAGE = """
        Test amd64 succeeded
        Test arm64 missing
        CHECKSUMS
        CHECKSUMS.gpg
        linux-headers-7.1.8-070108_7.1.8-070108.202608141745_all.deb
        linux-headers-7.1.8-070108-generic_7.1.8-070108.202608141745_amd64.deb
        linux-image-unsigned-7.1.8-070108-generic_7.1.8-070108.202608141745_amd64.deb
        linux-modules-7.1.8-070108-generic_7.1.8-070108.202608141745_amd64.deb
    """

    def test_only_complete_successful_amd64_build_is_installable(self):
        plan = mainline_package_plan("v7.1.8", self.COMPLETE_AMD64_PAGE, "amd64")

        self.assertIsNotNone(plan)
        self.assertEqual(plan.label, "Ubuntu Mainline v7.1.8")
        self.assertEqual(plan.source, "kernel.ubuntu.com")
        self.assertEqual(len(plan.packages), 4)

    def test_missing_or_arm64_mainline_build_is_not_offered(self):
        missing = self.COMPLETE_AMD64_PAGE.replace("Test amd64 succeeded", "Test amd64 missing")

        self.assertIsNone(mainline_package_plan("v7.1.8", missing, "amd64"))
        self.assertIsNone(mainline_package_plan("v7.1.8", self.COMPLETE_AMD64_PAGE, "arm64"))

    def test_checksum_manifest_must_cover_every_offered_mainline_package(self):
        plan = mainline_package_plan("v7.1.8", self.COMPLETE_AMD64_PAGE, "amd64")
        manifest = "\n".join(f"{'a' * 64}  {package}" for package in plan.packages)

        self.assertEqual(set(mainline_sha256sums(manifest, plan.packages)), set(plan.packages))
        self.assertIsNone(mainline_sha256sums(manifest.rsplit("\n", 1)[0], plan.packages))


class GrubParserTests(unittest.TestCase):
    def test_parser_returns_full_submenu_paths(self):
        config = """
            menuentry 'Ubuntu' {}
            submenu 'Advanced options for Ubuntu' {
                menuentry 'Ubuntu, with Linux 6.8.0-31-generic' {}
            }
        """

        self.assertEqual(
            parse_grub_menu_entries(config),
            (
                "Ubuntu",
                "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-31-generic",
            ),
        )

    def test_parser_keeps_submenu_open_across_menuentry_bodies(self):
        config = """
            submenu 'Advanced options for Ubuntu' {
                menuentry 'Ubuntu, with Linux 6.8.0-31-generic' {
                    echo 'boot 6.8'
                }
                menuentry 'Ubuntu, with Linux 6.8.0-30-generic' {}
            }
            menuentry 'Memory test' {}
        """

        self.assertEqual(
            parse_grub_menu_entries(config),
            (
                "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-31-generic",
                "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-30-generic",
                "Memory test",
            ),
        )

    def test_resolve_entry_only_accepts_a_full_discovered_path(self):
        entries = (
            "Ubuntu",
            "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-31-generic",
        )

        self.assertEqual(resolve_grub_entry(entries, entries[1]), entries[1])
        self.assertIsNone(resolve_grub_entry(entries, "1"))
        self.assertIsNone(resolve_grub_entry(entries, "Ubuntu, with Linux 6.8.0-31-generic"))


class KernelFactsTests(unittest.TestCase):
    def test_container_is_blocked_from_kernel_installation(self):
        facts = KernelFacts(
            identity=KernelIdentity("debian", "12", "bookworm", "amd64"),
            running_kernel="6.1.0-35-amd64",
            virtualization="docker",
            bootloader="none",
            boot_images=(),
            secure_boot="unknown",
            dkms_status=(),
            boot_free_bytes=0,
        )

        self.assertEqual(
            facts.installation_block_reason(),
            "容器共享宿主机内核，不能在容器内安装或切换内核。",
        )

    def test_supported_virtual_machine_with_grub_is_not_blocked(self):
        facts = KernelFacts(
            identity=KernelIdentity("ubuntu", "24.04", "noble", "arm64"),
            running_kernel="6.8.0-31-generic",
            virtualization="kvm",
            bootloader="grub",
            boot_images=("vmlinuz-6.8.0-31-generic",),
            secure_boot="disabled",
            dkms_status=(),
            boot_free_bytes=512 * 1024 * 1024,
        )

        self.assertIsNone(facts.installation_block_reason())


if __name__ == "__main__":
    unittest.main()
