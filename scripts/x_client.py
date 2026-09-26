"""X API v2 からツイート一覧とメトリクスを取得する層。

Tweepy を採用（理由: OAuth 1.0a ユーザーコンテキストの扱いが容易で、
v2 の tweet_fields / expansions を型安全に指定できるため）。

メトリクスの出どころ:
  - public_metrics       : like/reply/retweet/quote/impression（誰でも取得可）
  - non_public_metrics   : impression_count / url_link_clicks / user_profile_clicks
                           → 自分のツイートかつ OAuth ユーザーコンテキストが必要
  - organic_metrics      : impression + エンゲージ + url_link_clicks（ユーザーコンテキスト必要）

link_clicks は public_metrics には無いため、non_public/organic を要求する。
それらが 403 等で取れない環境では public_metrics のみに自動フォールバックする。

【重要】tweepy の Client メソッド（get_users_tweets / get_tweets 等）は、
Bearer と OAuth 1.0a のどちらでも呼べるエンドポイントに対して既定で
user_auth=False（Bearer優先）になっている。OAuth 1.0a の鍵しか渡していない
場合でも、user_auth=True を明示しないと認証ヘッダが正しく付与されず
401 Unauthorized になる（get_me() のように user_auth 一択のメソッドは対象外）。
そのため本モジュールでは is_user_context をそのまま user_auth に渡している。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import tweepy

from .config import XConfig
from .db import Metric, Post


# non_public / organic を要求するフィールドセット（ユーザーコンテキスト向け）
_FULL_TWEET_FIELDS = [
    "created_at",
    "public_metrics",
    "non_public_metrics",
    "organic_metrics",
    "attachments",
]
# フォールバック用（App-only Bearer でも通る）
_PUBLIC_TWEET_FIELDS = [
    "created_at",
    "public_metrics",
    "attachments",
]
_MEDIA_FIELDS = ["type"]
_EXPANSIONS = ["attachments.media_keys"]


@dataclass
class FetchResult:
    posts: list[Post]
    metrics: list[Metric]
    used_non_public: bool  # non_public/organic が取得できたか
    user_id: str


def _build_client(config: XConfig) -> tuple[tweepy.Client, bool]:
    """Tweepy Client を生成。(client, is_user_context) を返す。

    OAuth 1.0a ユーザーコンテキストを優先（non_public_metrics 取得のため）。
    無ければ Bearer にフォールバック（public_metrics のみ）。
    """
    if config.has_user_context:
        client = tweepy.Client(
            consumer_key=config.api_key,
            consumer_secret=config.api_secret,
            access_token=config.access_token,
            access_token_secret=config.access_token_secret,
        )
        return client, True
    if config.has_bearer:
        client = tweepy.Client(bearer_token=config.bearer_token)
        return client, False
    raise RuntimeError(
        "X API の認証情報が不足しています。OAuth 1.0a の鍵4点 "
        "(X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET) "
        "または X_API_BEARER_TOKEN を .env / Secrets に設定してください。"
    )


def _resolve_user_id(client: tweepy.Client, config: XConfig, is_user_context: bool) -> str:
    """対象ユーザー ID を解決する。"""
    if config.target_user_id:
        return config.target_user_id
    if is_user_context:
        me = client.get_me(user_auth=True)
        if me.data is None:
            raise RuntimeError("get_me() でユーザー情報を取得できませんでした。")
        return str(me.data.id)
    raise RuntimeError(
        "Bearer 認証のみの場合は X_TARGET_USER_ID の指定が必要です "
        "（get_me() はユーザーコンテキスト認証でのみ利用可能）。"
    )


def _media_maps(response) -> dict[str, str]:
    """media_key -> type のマップを includes から作る。"""
    result: dict[str, str] = {}
    includes = getattr(response, "includes", None) or {}
    for media in includes.get("media", []) or []:
        key = getattr(media, "media_key", None)
        mtype = getattr(media, "type", None)
        if key is not None:
            result[key] = mtype or ""
    return result


_IMAGE_TYPES = {"photo"}


def _to_post_and_metric(
    tweet,
    media_types: dict[str, str],
    fetched_date: str,
) -> tuple[Post, Metric]:
    """1件のツイートを Post / Metric に変換する。"""
    public = tweet.public_metrics or {}
    non_public = getattr(tweet, "non_public_metrics", None) or {}
    organic = getattr(tweet, "organic_metrics", None) or {}

    # インプレッション: non_public → organic → public の順で採用
    impressions = (
        non_public.get("impression_count")
        or organic.get("impression_count")
        or public.get("impression_count")
        or 0
    )
    # リンククリック: non_public → organic（public には存在しない）
    link_clicks = (
        non_public.get("url_link_clicks")
        or organic.get("url_link_clicks")
        or 0
    )

    # メディア判定
    media_keys = []
    attachments = getattr(tweet, "attachments", None) or {}
    if isinstance(attachments, dict):
        media_keys = attachments.get("media_keys", []) or []
    types = [media_types.get(k, "") for k in media_keys]
    media_count = len(media_keys)
    has_image = 1 if any(t in _IMAGE_TYPES for t in types) else 0

    posted_at = ""
    if getattr(tweet, "created_at", None) is not None:
        # tweepy は tz-aware datetime を返す。UTC ISO8601 で保存。
        posted_at = tweet.created_at.astimezone(dt.timezone.utc).isoformat()

    post = Post(
        tweet_id=str(tweet.id),
        posted_at=posted_at,
        text=tweet.text or "",
        has_image=has_image,
        media_count=media_count,
    )
    metric = Metric(
        tweet_id=str(tweet.id),
        fetched_date=fetched_date,
        impressions=int(impressions),
        likes=int(public.get("like_count", 0)),
        reposts=int(public.get("retweet_count", 0)),
        replies=int(public.get("reply_count", 0)),
        quotes=int(public.get("quote_count", 0)),
        link_clicks=int(link_clicks),
    )
    return post, metric


def _execute_with_fallback(is_user_context: bool, request_fn) -> tuple[object, bool]:
    """request_fn(tweet_fields) を実行し、(response, used_non_public) を返す。

    ユーザーコンテキストなら non_public/organic を含むフルフィールドで試み、
    403（権限不足等）の場合のみ public のみで再試行する。
    """
    if is_user_context:
        try:
            return request_fn(_FULL_TWEET_FIELDS), True
        except tweepy.errors.Forbidden:
            return request_fn(_PUBLIC_TWEET_FIELDS), False
    return request_fn(_PUBLIC_TWEET_FIELDS), False


def _response_to_result(
    response,
    fetched_date: str,
    used_non_public: bool,
    user_id: str,
) -> FetchResult:
    posts: list[Post] = []
    metrics: list[Metric] = []
    media_types = _media_maps(response)
    for tweet in response.data or []:
        post, metric = _to_post_and_metric(tweet, media_types, fetched_date)
        posts.append(post)
        metrics.append(metric)
    return FetchResult(
        posts=posts,
        metrics=metrics,
        used_non_public=used_non_public,
        user_id=user_id,
    )


def fetch_recent_metrics(
    config: XConfig,
    fetched_date: str | None = None,
) -> FetchResult:
    """【初回/ブートストラップ用】認証済みアカウントの直近ツイートを取得する。

    履歴が何もない初回実行や、疎通確認（test_connection）で使う。
    通常運用（2回目以降）の日次取得は fetch_new_since / fetch_by_ids を使う。

    Args:
        config: X API 設定。
        fetched_date: 観測日 'YYYY-MM-DD'。省略時は本日(UTC)。
    """
    if fetched_date is None:
        fetched_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    client, is_user_context = _build_client(config)
    user_id = _resolve_user_id(client, config, is_user_context)

    def _do_request(tweet_fields: list[str]):
        return client.get_users_tweets(
            id=user_id,
            max_results=config.max_results,
            tweet_fields=tweet_fields,
            media_fields=_MEDIA_FIELDS,
            expansions=_EXPANSIONS,
            exclude=["retweets", "replies"],  # 自アカの通常投稿に絞る
            user_auth=is_user_context,  # tweepyは既定でBearer側の認証を試みるため明示が必要
        )

    response, used_non_public = _execute_with_fallback(is_user_context, _do_request)
    return _response_to_result(response, fetched_date, used_non_public, user_id)


# get_users_tweets / get_tweets いずれも1回の呼び出しで最大100件まで。
# 日次で回す前提では since_id 以降の新規投稿が100件を超えることは
# 通常起こらないため単発呼び出しとするが、超過時は警告を出し取りこぼしを明示する。
_MAX_PAGE_SIZE = 100


def fetch_new_since(
    config: XConfig,
    since_id: str,
    fetched_date: str | None = None,
) -> FetchResult:
    """since_id より新しいツイート（前回取得以降の新規投稿）のみを取得する。

    Args:
        config: X API 設定。
        since_id: この tweet_id より新しいものだけを取得する（DB内の最新ID）。
        fetched_date: 観測日 'YYYY-MM-DD'。省略時は本日(UTC)。
    """
    if fetched_date is None:
        fetched_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    client, is_user_context = _build_client(config)
    user_id = _resolve_user_id(client, config, is_user_context)

    def _do_request(tweet_fields: list[str]):
        return client.get_users_tweets(
            id=user_id,
            since_id=since_id,
            max_results=_MAX_PAGE_SIZE,
            tweet_fields=tweet_fields,
            media_fields=_MEDIA_FIELDS,
            expansions=_EXPANSIONS,
            exclude=["retweets", "replies"],
            user_auth=is_user_context,
        )

    response, used_non_public = _execute_with_fallback(is_user_context, _do_request)

    meta = getattr(response, "meta", None) or {}
    if meta.get("next_token"):
        count = meta.get("result_count", "?")
        print(
            f"[x_client] 警告: since_id={since_id} 以降の新規投稿が{_MAX_PAGE_SIZE}件を超えています "
            f"（今回は先頭{count}件のみ取得。取りこぼしがある可能性があります）。",
        )

    return _response_to_result(response, fetched_date, used_non_public, user_id)


def fetch_by_ids(
    config: XConfig,
    tweet_ids: list[str],
    fetched_date: str | None = None,
) -> FetchResult:
    """指定した tweet_id 群の最新メトリクスを再取得する（既存投稿の成長追跡用）。

    Args:
        config: X API 設定。
        tweet_ids: 再取得したいツイート ID のリスト（最大100件）。
        fetched_date: 観測日 'YYYY-MM-DD'。省略時は本日(UTC)。
    """
    if fetched_date is None:
        fetched_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if not tweet_ids:
        return FetchResult(posts=[], metrics=[], used_non_public=False, user_id="")
    if len(tweet_ids) > _MAX_PAGE_SIZE:
        raise ValueError(f"tweet_ids は最大{_MAX_PAGE_SIZE}件までです（{len(tweet_ids)}件指定）")

    client, is_user_context = _build_client(config)
    # user_id は解決できれば付与するが、必須ではない（ID指定取得のため）
    try:
        user_id = _resolve_user_id(client, config, is_user_context)
    except RuntimeError:
        user_id = ""

    def _do_request(tweet_fields: list[str]):
        return client.get_tweets(
            ids=tweet_ids,
            tweet_fields=tweet_fields,
            media_fields=_MEDIA_FIELDS,
            expansions=_EXPANSIONS,
            user_auth=is_user_context,
        )

    response, used_non_public = _execute_with_fallback(is_user_context, _do_request)
    return _response_to_result(response, fetched_date, used_non_public, user_id)


def merge_results(*results: FetchResult) -> FetchResult:
    """複数の FetchResult を tweet_id で重複排除しつつ統合する。

    新規取得（fetch_new_since）と既存再取得（fetch_by_ids）で対象が
    重なるケース（境界付近のツイート）があるため、後勝ちでまとめる。
    """
    posts_by_id: dict[str, Post] = {}
    metrics_by_id: dict[str, Metric] = {}
    used_non_public = False
    user_id = ""
    for result in results:
        for post in result.posts:
            posts_by_id[post.tweet_id] = post
        for metric in result.metrics:
            metrics_by_id[metric.tweet_id] = metric
        used_non_public = used_non_public or result.used_non_public
        user_id = result.user_id or user_id

    return FetchResult(
        posts=list(posts_by_id.values()),
        metrics=list(metrics_by_id.values()),
        used_non_public=used_non_public,
        user_id=user_id,
    )
