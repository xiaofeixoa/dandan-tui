"""nginx_manager 纯函数与写入守卫测试（不需要 nginx / root）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nginx_manager


SAMPLE = """# configuration file /etc/nginx/conf.d/a.conf:
server {
    listen 10301 ssl;
    listen [::]:10301;
    server_name a.example.com;
    root /var/www/a;
    ssl_certificate /etc/letsencrypt/live/a/fullchain.pem;
}
# configuration file /etc/nginx/conf.d/b.conf:
server {
    listen 10302;
    server_name b.example.com;
    location / { proxy_pass http://127.0.0.1:9000; }
    location /api/ { proxy_pass http://127.0.0.1:9001; }
}
"""


class ParserTests(unittest.TestCase):
    def test_parse_sites_extracts_directives(self):
        sites = nginx_manager.parse_sites(SAMPLE)
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0].config_path, "/etc/nginx/conf.d/a.conf")
        self.assertEqual(sites[0].server_name, ["a.example.com"])
        # listen 抓取整段参数；端口检测由 parse_listeners 基于 ss 输出完成。
        self.assertEqual(sites[0].listen, ["10301", "ssl"])
        self.assertEqual(sites[0].certificate, "/etc/letsencrypt/live/a/fullchain.pem")
        self.assertEqual(sites[1].proxy_pass, "http://127.0.0.1:9000")
        self.assertEqual(len(sites[1].locations), 2)
        self.assertIn("10301", sites[0].label)
        self.assertIn("a.example.com", sites[0].label)

    def test_parse_listeners_covers_ipv4_and_ipv6(self):
        text = "tcp LISTEN 0 128 0.0.0.0:10305 0.0.0.0:*\ntcp LISTEN 0 128 [::]:443 [::]:*\n"
        self.assertEqual(nginx_manager.parse_listeners(text), {"10305", "443"})

    def test_valid_domain_rejects_bad_shapes(self):
        for good in ("a.example.com", "*.example.com", "a-b.example.com"):
            self.assertTrue(nginx_manager.valid_domain(good), good)
        for bad in ("localhost", "-a.example.com", "a..example.com", "not a domain", "a.example.com;"):
            self.assertFalse(nginx_manager.valid_domain(bad), bad)

    def test_valid_port_bounds(self):
        self.assertTrue(nginx_manager.valid_port("1") and nginx_manager.valid_port("65535"))
        for bad in ("0", "65536", "", "10305x"):
            self.assertFalse(nginx_manager.valid_port(bad), bad)

    def test_valid_web_root_allows_common_and_blocks_system_paths(self):
        for good in ("/var/www/site", "/srv/www", "/home/web/static", "/data/site/"):
            self.assertTrue(nginx_manager.valid_web_root(good), good)
        for bad in ("/", "/var", "/etc", "/etc/nginx", "/boot/grub", "/usr/share", "/var/log", "/var/lib", "relative", "/var/www;rm"):
            self.assertFalse(nginx_manager.valid_web_root(bad), bad)

    def test_static_site_config_uses_given_values(self):
        content = nginx_manager.static_site_config("s.example.com", "10310", "/var/www/s")
        self.assertIn("listen 10310;", content)
        self.assertIn("server_name s.example.com;", content)
        self.assertIn("root /var/www/s;", content)


class WriteNewConfGuardTests(unittest.TestCase):
    def setUp(self):
        self.manager = nginx_manager.NginxManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_rejects_without_root(self):
        with mock.patch("nginx_manager._is_root", return_value=False), \
             mock.patch("sys.stdout"):
            self.assertFalse(self.manager._write_new_conf(self.tmp / "a.conf", "server {}"))

    def test_rejects_existing_target(self):
        path = self.tmp / "a.conf"
        path.write_text("existing", encoding="utf-8")
        with mock.patch("nginx_manager._is_root", return_value=True), \
             mock.patch("sys.stdout"):
            self.assertFalse(self.manager._write_new_conf(path, "new"))

    def test_rejects_paths_outside_conf_d(self):
        path = self.tmp / "sub" / "a.conf"
        path.parent.mkdir()
        with mock.patch("nginx_manager._is_root", return_value=True), \
             mock.patch("sys.stdout"):
            self.assertFalse(self.manager._write_new_conf(path, "new"))


if __name__ == "__main__":
    unittest.main()
