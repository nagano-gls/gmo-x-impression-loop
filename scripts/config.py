"""環境変数の読み込みと設定値の一元管理。

ローカル実行時は `.env` を自動読み込みする（python-dotenv があれば）。
GitHub Actions 上では Secrets が環境変数として注入されるため .env は不要。
認証情報はここでのみ環境変数から取得し、他モジュールへ渡す。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --- .env の読み込み（存在すれば）。CI では dotenv 未導入でも動くよう握りつぶす ---
try:
    from dotenv import load_dotenv

    # リポジトリ直下の .env を明示的に指す（実行ディレクトリに依存しない）
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=_ENV_PATH)
except ImportError:  # pragma: no cover - CI 環境など
    pass


# リポジトリのルート（scripts/ の1つ上）
REPO_ROOT = Path(__file__).resolve().parent.parent


def _get(name: str, default: str | None = None) -> str | None:
    """環境変数を取得。空文字は未設定として扱う。"""
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or None


@dataclass(frozen=True)
class XConfig:
    """X API の認証・取得設定。"""

    api_key: str | None
    api_secret: str | None
    access_token: str | None
    access_token_secret: str | None
    bearer_token: str | None
    target_user_id: str | None
    max_results: int
    refresh_count: int

    @property
    def has_user_context(self) -> bool:
        """OAuth 1.0a ユーザーコンテキスト（鍵4点）が揃っているか。"""
        return all(
            [
                self.api_key,
                self.api_secret,
                self.access_token,
                self.access_token_secret,
            ]
        )

    @property
    def has_bearer(self) -> bool:
        return bool(self.bearer_token)


def load_x_config() -> XConfig:
    """環境変数から X API 設定を組み立てる。"""
    raw_max = _get("X_MAX_RESULTS", "50") or "50"
    try:
        max_results = int(raw_max)
    except ValueError:
        max_results = 50
    # X API v2 の get_users_tweets は 5〜100 の範囲
    max_results = max(5, min(100, max_results))

    raw_refresh = _get("X_REFRESH_COUNT", "15") or "15"
    try:
        refresh_count = int(raw_refresh)
    except ValueError:
        refresh_count = 15
    # get_tweets(ids=...) は最大100件/回
    refresh_count = max(0, min(100, refresh_count))

    return XConfig(
        api_key=_get("X_API_KEY"),
        api_secret=_get("X_API_SECRET"),
        access_token=_get("X_ACCESS_TOKEN"),
        access_token_secret=_get("X_ACCESS_TOKEN_SECRET"),
        bearer_token=_get("X_API_BEARER_TOKEN"),
        target_user_id=_get("X_TARGET_USER_ID"),
        max_results=max_results,
        refresh_count=refresh_count,
    )


def get_db_path() -> Path:
    """SQLite DB の絶対パスを返す。相対指定はリポジトリルート基準。"""
    raw = _get("DB_PATH", "data/impressions.db") or "data/impressions.db"
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


# --------------------------------------------------------------------------- #
# フェーズ2: 月次分析・Slack通知で使用
# --------------------------------------------------------------------------- #
def get_anthropic_api_key() -> str | None:
    return _get("ANTHROPIC_API_KEY")


def get_anthropic_model() -> str:
    """月次分析で使う Claude のモデル名（環境変数で上書き可能）。"""
    return _get("ANTHROPIC_MODEL", "claude-sonnet-5") or "claude-sonnet-5"


def get_slack_webhook_url() -> str | None:
    return _get("SLACK_WEBHOOK_URL")


def get_reports_dir() -> Path:
    """月次分析レポートの保存先ディレクトリ（既定: data/reports）。"""
    raw = _get("REPORTS_DIR", "data/reports") or "data/reports"
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path
