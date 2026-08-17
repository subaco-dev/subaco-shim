"""tls.py の証明書生成・certifi 結合バンドル・再生成条件。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from subaco_shim.config import CubePaths
from subaco_shim.tls import TlsSetupError, ensure_tls_material

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="TLS 証明書生成に openssl CLI が必要"
)


def test_bundle_combines_certifi_and_shim_cert(tmp_path):
    """結合バンドルは certifi 全体 + シム証明書を含む（シム証明書単体は通常 HTTPS を壊す）。"""
    import certifi

    material = ensure_tls_material(CubePaths.resolve(tmp_path))
    bundle = material.ca_bundle.read_bytes()
    assert Path(certifi.where()).read_bytes() in bundle
    assert material.cert.read_bytes() in bundle


def test_material_reused_across_calls(tmp_path):
    paths = CubePaths.resolve(tmp_path)
    m1 = ensure_tls_material(paths)
    cert_bytes = m1.cert.read_bytes()
    m2 = ensure_tls_material(paths)
    # 有効期限内の証明書は再生成されない（プロジェクト永続・SDK の SSL context と整合）。
    assert m2.cert.read_bytes() == cert_bytes


def test_legacy_cert_only_bundle_is_migrated(tmp_path):
    """旧版が生成した「シム証明書だけのバンドル」は mtime によらず再生成される。

    レビュー指摘の再現: mtime 比較では、旧バンドルが certifi ファイルより新しい場合に
    移行されない。判定は内容包含（certifi 全体 + 現行証明書）で行う。
    """
    import certifi

    paths = CubePaths.resolve(tmp_path)
    m = ensure_tls_material(paths)
    # 旧版成果物を模擬: certifi を含まない cert 単体バンドル（mtime は最新 = 罠の条件）。
    m.ca_bundle.write_bytes(m.cert.read_bytes())
    m2 = ensure_tls_material(paths)
    bundle = m2.ca_bundle.read_bytes()
    assert Path(certifi.where()).read_bytes() in bundle
    assert m2.cert.read_bytes() in bundle


def test_bundle_regenerated_when_content_stale(tmp_path):
    """内容が現行ソース（certifi + 証明書）を含まなければ再生成する（証明書更新への追随）。"""
    paths = CubePaths.resolve(tmp_path)
    m = ensure_tls_material(paths)
    # 証明書だけ差し替わった状況を模擬（バンドルは旧証明書のまま）。
    stale = m.ca_bundle.read_bytes().replace(m.cert.read_bytes(), b"")
    m.ca_bundle.write_bytes(stale)
    m2 = ensure_tls_material(paths)
    assert m2.cert.read_bytes() in m2.ca_bundle.read_bytes()


def test_current_bundle_is_not_rewritten(tmp_path):
    """内容が最新ならバンドルは書き換えない（mtime 非依存の安定判定）。"""
    paths = CubePaths.resolve(tmp_path)
    m = ensure_tls_material(paths)
    os.utime(m.ca_bundle, (1, 1))  # mtime を大昔にしても内容が最新なら再生成不要。
    ensure_tls_material(paths)
    assert m.ca_bundle.stat().st_mtime == 1


def test_missing_certifi_fails_closed(tmp_path, monkeypatch):
    """certifi 不在は起動失敗（黙ってシム証明書単体のバンドルを作らない）。"""
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi を ImportError にする。
    with pytest.raises(TlsSetupError):
        ensure_tls_material(CubePaths.resolve(tmp_path))


def test_key_permissions(tmp_path):
    material = ensure_tls_material(CubePaths.resolve(tmp_path))
    assert (material.key.stat().st_mode & 0o777) == 0o600
