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


def test_bundle_regenerated_when_stale(tmp_path):
    """バンドルが証明書／certifi より古ければ再生成する（certifi 更新への追随）。"""
    paths = CubePaths.resolve(tmp_path)
    m = ensure_tls_material(paths)
    os.utime(m.ca_bundle, (1, 1))  # certifi の CA ファイルより確実に古くする。
    ensure_tls_material(paths)
    assert m.ca_bundle.stat().st_mtime > 1


def test_missing_certifi_fails_closed(tmp_path, monkeypatch):
    """certifi 不在は起動失敗（黙ってシム証明書単体のバンドルを作らない）。"""
    monkeypatch.setitem(sys.modules, "certifi", None)  # import certifi を ImportError にする。
    with pytest.raises(TlsSetupError):
        ensure_tls_material(CubePaths.resolve(tmp_path))


def test_key_permissions(tmp_path):
    material = ensure_tls_material(CubePaths.resolve(tmp_path))
    assert (material.key.stat().st_mode & 0o777) == 0o600
