"""データプレーン TLS 資材（``*.sbx.localhost`` ワイルドカード証明書）の生成と再利用。

spike §2(e) の確定方式:

- **3 ラベルワイルドカード** ``*.sbx.localhost`` の自己署名証明書（OpenSSL は 2 ラベル
  ``*.localhost`` を拒否する——実測 ``CERTIFICATE_VERIFY_FAILED``）。
- 初回生成し ``.cube/tls/`` に保存（鍵 0600）。以後は再利用し、失効が近づいたら再生成する。
- ``SSL_CERT_FILE`` は CA バンドル全体を**置き換える**ため、SDK 側プロセスの他の HTTPS を
  壊さないよう **certifi + シム証明書の結合バンドル**（``ca-bundle.pem``）を生成する。
  certifi は**実行時必須依存**（pyproject）であり、不在時は :class:`TlsSetupError` で
  起動失敗させる — シム証明書単体のバンドルを黙って作ると、それを ``SSL_CERT_FILE`` に
  設定した SDK 側プロセスの通常 HTTPS が全滅するため。certifi 更新時（パッケージ更新で
  CA ファイルが新しくなった時）はバンドルを再生成する。

証明書生成は ``openssl`` CLI に依存する（stdlib に証明書生成 API はない。Nix devShell /
CI には openssl が入る前提。不在時は :class:`TlsSetupError` で明示エラー）。
サーバー側の SSLContext は ALPN を広告しない（h2 を広告すると SDK の共有トランスポート
``http2=True`` が HTTP/2 を要求してしまう——spike §2(e)-5）。
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CUBE_TOKEN_MODE, CubePaths
from .logging import get_logger
from .protocol.wire import SANDBOX_DOMAIN_BASE

_log = get_logger("tls")

# 証明書の有効日数と、再生成を始める残余秒数（30 日を切ったら作り直す）。
_CERT_DAYS = 825
_RENEW_MARGIN_SECONDS = 30 * 24 * 3600

_WILDCARD = f"*.{SANDBOX_DOMAIN_BASE}"


class TlsSetupError(RuntimeError):
    """TLS 資材を用意できない（openssl 不在・生成失敗）。"""


@dataclass(frozen=True)
class TlsMaterial:
    """生成済み TLS 資材のパス集合。"""

    cert: Path  # サーバー証明書（自己署名・*.sbx.localhost）
    key: Path  # 秘密鍵（0600）
    ca_bundle: Path  # certifi + cert の結合バンドル（SSL_CERT_FILE 用）

    def server_context(self) -> ssl.SSLContext:
        """データプレーンリスナー用の SSLContext を返す（ALPN h2 非広告）。"""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.cert), str(self.key))
        # set_alpn_protocols は呼ばない——ALPN 無応答で SDK は HTTP/1.1 にフォールバックする。
        return ctx


def _openssl() -> str:
    exe = shutil.which("openssl")
    if not exe:
        raise TlsSetupError(
            "openssl が見つかりません。データプレーン TLS 証明書の生成に必要です"
            "（Nix devShell / パッケージマネージャで openssl を導入してください）"
        )
    return exe


def _cert_needs_renewal(cert: Path) -> bool:
    """証明書の失効が近いか（openssl x509 -checkend。判定不能時は再生成に倒す）。"""
    try:
        argv = [_openssl(), "x509", "-checkend", str(_RENEW_MARGIN_SECONDS), "-noout"]
        proc = subprocess.run([*argv, "-in", str(cert)], capture_output=True)
    except OSError:
        return True
    return proc.returncode != 0


def _generate(cert: Path, key: Path) -> None:
    """ワイルドカード自己署名証明書を生成する（spike と同一パラメータ・EC P-256）。"""
    try:
        subprocess.run(
            [
                _openssl(),
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                str(_CERT_DAYS),
                "-nodes",
                "-subj",
                f"/CN={_WILDCARD}",
                "-addext",
                f"subjectAltName=DNS:{_WILDCARD}",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise TlsSetupError(
            f"TLS 証明書の生成に失敗しました: {exc.stderr.decode(errors='replace').strip()}"
        ) from exc
    key.chmod(CUBE_TOKEN_MODE)


def _certifi_ca_path() -> Path:
    """certifi の CA バンドルパスを返す（不在は :class:`TlsSetupError` — 必須依存）。"""
    try:
        import certifi
    except ImportError as exc:
        raise TlsSetupError(
            "certifi が見つかりません。SSL_CERT_FILE 用の結合 CA バンドル生成に必須です"
            "（シム証明書単体のバンドルは SDK 側プロセスの通常 HTTPS を壊すため、"
            "certifi なしでは起動しません）"
        ) from exc
    return Path(certifi.where())


def _write_bundle(certifi_ca: Path, cert: Path, bundle: Path) -> None:
    """certifi + シム証明書の結合バンドルを書く。"""
    bundle.write_bytes(certifi_ca.read_bytes() + b"\n" + cert.read_bytes())


def _bundle_is_current(bundle: Path, certifi_ca: Path, cert: Path) -> bool:
    """バンドルが「現行 certifi の全内容 + 現行シム証明書」を含むかを**内容で**判定する。

    mtime 比較では、旧版が生成した「シム証明書だけのバンドル」（certifi 任意依存時代の
    成果物）が certifi ファイルより新しい場合に移行されない。内容包含の検査なら
    旧成果物・certifi パッケージ更新・証明書再生成のすべてで正しく再生成に倒れる。
    """
    if not bundle.is_file():
        return False
    try:
        data = bundle.read_bytes()
    except OSError:
        return False
    return certifi_ca.read_bytes() in data and cert.read_bytes() in data


def ensure_tls_material(paths: CubePaths) -> TlsMaterial:
    """``.cube/tls/`` の証明書・鍵・結合バンドルを用意する（再利用・失効前再生成）。"""
    paths.ensure_dir()
    paths.tls_dir.mkdir(mode=0o700, exist_ok=True)
    cert, key, bundle = paths.tls_cert, paths.tls_key, paths.tls_ca_bundle
    certifi_ca = _certifi_ca_path()

    if not (cert.is_file() and key.is_file()) or _cert_needs_renewal(cert):
        _generate(cert, key)
        _log.info("tls_cert_generated cert=%s days=%s", cert, _CERT_DAYS)
    if not _bundle_is_current(bundle, certifi_ca, cert):
        _write_bundle(certifi_ca, cert, bundle)
        _log.info("ca_bundle_written bundle=%s certifi=%s", bundle, certifi_ca)
    return TlsMaterial(cert=cert, key=key, ca_bundle=bundle)
