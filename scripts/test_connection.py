"""ローカル疎通確認スクリプト（フェーズ1の動作確認用）。

手元で `.env` に API キーを設定し、次を実行:
    python -m scripts.test_connection

やること:
  1. 認証情報の有無を確認して、どの認証方式で動くかを表示
  2. X API から自分のアカウントの直近ツイートを取得（DB には書き込まない dry-run）
  3. 1件でも取れれば代表サンプルを表示し、SUCCESS を返す
  4. --save を付けると SQLite への保存まで通しで確認する
"""

from __future__ import annotations

import argparse
import sys

from . import config as cfg
from . import db
from .x_client import fetch_recent_metrics


def _mask(value: str | None) -> str:
    if not value:
        return "(未設定)"
    if len(value) <= 6:
        return "******"
    return f"{value[:3]}…{value[-3:]}"


def _print_auth_summary(x_config) -> None:
    print("=== 認証情報の確認 ===")
    print(f"  X_API_KEY:              {_mask(x_config.api_key)}")
    print(f"  X_API_SECRET:           {_mask(x_config.api_secret)}")
    print(f"  X_ACCESS_TOKEN:         {_mask(x_config.access_token)}")
    print(f"  X_ACCESS_TOKEN_SECRET:  {_mask(x_config.access_token_secret)}")
    print(f"  X_API_BEARER_TOKEN:     {_mask(x_config.bearer_token)}")
    print(f"  X_TARGET_USER_ID:       {x_config.target_user_id or '(未設定 → get_me() で解決)'}")
    print(f"  max_results:            {x_config.max_results}")
    if x_config.has_user_context:
        print("  → OAuth 1.0a ユーザーコンテキストで動作（リンククリック等も取得試行）")
    elif x_config.has_bearer:
        print("  → Bearer 認証で動作（public_metrics のみ／要 X_TARGET_USER_ID）")
    else:
        print("  → 認証情報が不足しています。.env を確認してください。")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="X API 疎通確認")
    parser.add_argument("--save", action="store_true", help="取得結果を SQLite に保存する")
    args = parser.parse_args()

    x_config = cfg.load_x_config()
    _print_auth_summary(x_config)

    if not (x_config.has_user_context or x_config.has_bearer):
        print("FAILURE: 認証情報が設定されていません。", file=sys.stderr)
        return 1

    print("=== X API からの取得を試行 ===")
    try:
        result = fetch_recent_metrics(x_config)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILURE: 取得に失敗しました: {exc}", file=sys.stderr)
        return 1

    n = len(result.metrics)
    kind = "non_public/organic" if result.used_non_public else "public のみ"
    print(f"  user_id: {result.user_id}")
    print(f"  取得件数: {n} 件（メトリクス種別: {kind}）")

    if n == 0:
        print("\nWARNING: 認証は成功しましたが、対象ツイートが0件でした。")
        print("         直近に投稿があるか、exclude 条件やアカウントをご確認ください。")
        return 0

    # 代表サンプル（最大3件）を表示
    print("\n=== 取得サンプル（先頭3件） ===")
    by_id = {p.tweet_id: p for p in result.posts}
    for metric in result.metrics[:3]:
        post = by_id.get(metric.tweet_id)
        text_preview = (post.text[:40] + "…") if post and len(post.text) > 40 else (post.text if post else "")
        text_preview = text_preview.replace("\n", " ")
        print(f"  - tweet_id={metric.tweet_id} posted_at={post.posted_at if post else '?'}")
        print(f"    text: {text_preview}")
        print(
            f"    impressions={metric.impressions} likes={metric.likes} "
            f"reposts={metric.reposts} replies={metric.replies} "
            f"quotes={metric.quotes} link_clicks={metric.link_clicks} "
            f"has_image={post.has_image if post else '?'} media={post.media_count if post else '?'}"
        )

    if args.save:
        db_path = cfg.get_db_path()
        with db.connect(db_path) as conn:
            db.init_db(conn)
            n_posts, n_metrics = db.save_batch(conn, result.posts, result.metrics)
        print(f"\n=== SQLite 保存 ===")
        print(f"  保存先: {db_path}")
        print(f"  保存件数: posts={n_posts}, metrics={n_metrics}")

    print("\nSUCCESS: X API からデータを取得できました。")
    if not result.used_non_public:
        print("NOTE: リンククリック数(link_clicks)は non_public_metrics のため今回は0固定です。")
        print("      取得するには OAuth 1.0a ユーザーコンテキスト（鍵4点）で実行してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
