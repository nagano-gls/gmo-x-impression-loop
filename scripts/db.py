"""SQLite への保存層。

テーブル設計:
  posts  ... ツイート本体（1ツイート1行、不変に近い属性）
  metrics ... 日次スナップショット（tweet_id × fetched_date で1行）

同じ日に複数回実行しても重複しないよう UPSERT で書き込む。
metrics は (tweet_id, fetched_date) を主キーにすることで、
「1日1回の観測値」を上書き更新する（＝日次で時系列が積み上がる）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator


# --------------------------------------------------------------------------- #
# データモデル
# --------------------------------------------------------------------------- #
@dataclass
class Post:
    tweet_id: str
    posted_at: str  # ISO8601 (UTC)
    text: str
    has_image: int  # 0 / 1
    media_count: int


@dataclass
class Metric:
    tweet_id: str
    fetched_date: str  # 'YYYY-MM-DD'
    impressions: int
    likes: int
    reposts: int
    replies: int
    quotes: int
    link_clicks: int


# --------------------------------------------------------------------------- #
# スキーマ
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    tweet_id    TEXT PRIMARY KEY,
    posted_at   TEXT NOT NULL,
    text        TEXT NOT NULL,
    has_image   INTEGER NOT NULL DEFAULT 0,
    media_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    tweet_id     TEXT NOT NULL,
    fetched_date TEXT NOT NULL,
    impressions  INTEGER NOT NULL DEFAULT 0,
    likes        INTEGER NOT NULL DEFAULT 0,
    reposts      INTEGER NOT NULL DEFAULT 0,
    replies      INTEGER NOT NULL DEFAULT 0,
    quotes       INTEGER NOT NULL DEFAULT 0,
    link_clicks  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tweet_id, fetched_date),
    FOREIGN KEY (tweet_id) REFERENCES posts (tweet_id)
);

CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics (fetched_date);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts (posted_at);
"""


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """接続を開き、外部キーを有効化して yield。終了時に close。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """テーブルとインデックスを作成（存在すれば何もしない）。"""
    conn.executescript(_SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- #
# 書き込み（UPSERT）
# --------------------------------------------------------------------------- #
def upsert_post(conn: sqlite3.Connection, post: Post) -> None:
    conn.execute(
        """
        INSERT INTO posts (tweet_id, posted_at, text, has_image, media_count)
        VALUES (:tweet_id, :posted_at, :text, :has_image, :media_count)
        ON CONFLICT(tweet_id) DO UPDATE SET
            posted_at   = excluded.posted_at,
            text        = excluded.text,
            has_image   = excluded.has_image,
            media_count = excluded.media_count;
        """,
        asdict(post),
    )


def upsert_metric(conn: sqlite3.Connection, metric: Metric) -> None:
    conn.execute(
        """
        INSERT INTO metrics (
            tweet_id, fetched_date, impressions, likes,
            reposts, replies, quotes, link_clicks
        )
        VALUES (
            :tweet_id, :fetched_date, :impressions, :likes,
            :reposts, :replies, :quotes, :link_clicks
        )
        ON CONFLICT(tweet_id, fetched_date) DO UPDATE SET
            impressions = excluded.impressions,
            likes       = excluded.likes,
            reposts     = excluded.reposts,
            replies     = excluded.replies,
            quotes      = excluded.quotes,
            link_clicks = excluded.link_clicks;
        """,
        asdict(metric),
    )


def save_batch(
    conn: sqlite3.Connection,
    posts: Iterable[Post],
    metrics: Iterable[Metric],
) -> tuple[int, int]:
    """複数の post / metric をまとめて UPSERT。件数を返す。"""
    n_posts = 0
    for post in posts:
        upsert_post(conn, post)
        n_posts += 1
    n_metrics = 0
    for metric in metrics:
        upsert_metric(conn, metric)
        n_metrics += 1
    conn.commit()
    return n_posts, n_metrics


# --------------------------------------------------------------------------- #
# 参照（テストや確認・差分取得の判定用）
# --------------------------------------------------------------------------- #
def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in ("posts", "metrics"):
        raise ValueError(f"unknown table: {table}")
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"])


def get_latest_tweet_id(conn: sqlite3.Connection) -> str | None:
    """DB に保存済みの中で最も新しい tweet_id を返す（since_id 用）。

    ツイート ID は Snowflake（時系列で単調増加する数値）のため、
    数値として比較することで最新の1件を特定できる。1件もなければ None
    （＝初回実行として扱う）。
    """
    row = conn.execute(
        "SELECT tweet_id FROM posts ORDER BY CAST(tweet_id AS INTEGER) DESC LIMIT 1"
    ).fetchone()
    return row["tweet_id"] if row else None


def get_recent_tweet_ids(conn: sqlite3.Connection, limit: int) -> list[str]:
    """投稿日時が新しい順に tweet_id を limit 件返す（既存投稿の再取得対象）。"""
    rows = conn.execute(
        "SELECT tweet_id FROM posts ORDER BY posted_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row["tweet_id"] for row in rows]


# --------------------------------------------------------------------------- #
# 月次分析用（フェーズ2）
# --------------------------------------------------------------------------- #
def get_month_posts_with_latest_metrics(
    conn: sqlite3.Connection,
    start_inclusive_iso: str,
    end_exclusive_iso: str,
) -> list[sqlite3.Row]:
    """指定期間 [start, end) に投稿されたツイートを、各ツイートの最新メトリクス付きで返す。

    1ツイートに複数日分の metrics 行があるため、fetched_date が最も新しい
    行（＝直近の観測値）を「その投稿の代表値」として採用する。
    posted_at 昇順で返す。
    """
    rows = conn.execute(
        """
        SELECT
            p.tweet_id, p.posted_at, p.text, p.has_image, p.media_count,
            m.fetched_date, m.impressions, m.likes, m.reposts,
            m.replies, m.quotes, m.link_clicks
        FROM posts p
        JOIN metrics m ON m.tweet_id = p.tweet_id
        JOIN (
            SELECT tweet_id, MAX(fetched_date) AS max_date
            FROM metrics
            GROUP BY tweet_id
        ) latest ON latest.tweet_id = m.tweet_id AND latest.max_date = m.fetched_date
        WHERE p.posted_at >= ? AND p.posted_at < ?
        ORDER BY p.posted_at ASC;
        """,
        (start_inclusive_iso, end_exclusive_iso),
    ).fetchall()
    return rows
