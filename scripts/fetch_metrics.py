"""日次ジョブのエントリポイント。

X API から「前回取得以降の新規投稿」＋「既存投稿のうち直近N件（既定15件）」の
メトリクスを取得し、SQLite へ UPSERT で保存する。
GitHub Actions からは `python -m scripts.fetch_metrics` で呼ぶ想定。

このスクリプト自体は「取得→保存」だけを行い、副作用（投稿など）は持たない。

取得方針:
  - 履歴が無い初回実行        : ブートストラップとして直近 max_results 件を取得
  - 2回目以降（履歴あり）      : since_id（DB内最新tweet_id）より新しい投稿 ＋
                                投稿日時が新しい順の既存投稿 refresh_count 件を
                                再取得し、伸び（インプレッションの推移）を追跡する
"""

from __future__ import annotations

import argparse
import sys

from . import config as cfg
from . import db
from .x_client import fetch_by_ids, fetch_new_since, fetch_recent_metrics, merge_results


def run(fetched_date: str | None = None) -> int:
    """取得と保存を実行し、保存件数(metrics)を返す。"""
    x_config = cfg.load_x_config()
    db_path = cfg.get_db_path()

    with db.connect(db_path) as conn:
        db.init_db(conn)
        since_id = db.get_latest_tweet_id(conn)
        recent_ids = db.get_recent_tweet_ids(conn, x_config.refresh_count) if since_id else []

    if since_id is None:
        print("[fetch_metrics] 初回実行のため、履歴なしブートストラップ取得を行います。")
        result = fetch_recent_metrics(x_config, fetched_date=fetched_date)
    else:
        print(
            f"[fetch_metrics] 差分取得: since_id={since_id} より新しい投稿 ＋ "
            f"既存直近{len(recent_ids)}件（設定上限{x_config.refresh_count}件）を再取得します。"
        )
        new_result = fetch_new_since(x_config, since_id, fetched_date=fetched_date)
        if recent_ids:
            refresh_result = fetch_by_ids(x_config, recent_ids, fetched_date=fetched_date)
            result = merge_results(new_result, refresh_result)
        else:
            result = new_result
        print(
            f"[fetch_metrics] 新規投稿: {len(new_result.posts)}件 / "
            f"既存再取得: {len(recent_ids)}件 / 統合後: {len(result.posts)}件"
        )

    with db.connect(db_path) as conn:
        db.init_db(conn)
        n_posts, n_metrics = db.save_batch(conn, result.posts, result.metrics)
        total_posts = db.count_rows(conn, "posts")
        total_metrics = db.count_rows(conn, "metrics")

    metric_kind = "non_public/organic（インプレ+クリック）" if result.used_non_public else "public のみ（クリック取得不可）"
    print(f"[fetch_metrics] user_id={result.user_id}")
    print(f"[fetch_metrics] 取得メトリクス種別: {metric_kind}")
    print(f"[fetch_metrics] 今回保存: posts={n_posts}, metrics={n_metrics}")
    print(f"[fetch_metrics] DB累計: posts={total_posts}, metrics={total_metrics}")
    print(f"[fetch_metrics] DB: {db_path}")
    return n_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="X の直近メトリクスを取得して SQLite に保存する")
    parser.add_argument(
        "--date",
        dest="date",
        default=None,
        help="観測日 YYYY-MM-DD（省略時は本日 UTC）",
    )
    args = parser.parse_args()

    try:
        run(fetched_date=args.date)
    except Exception as exc:  # noqa: BLE001 - CI ログに要因を残して非0終了
        print(f"[fetch_metrics] エラー: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
