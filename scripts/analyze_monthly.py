"""月次ジョブ: その月の投稿データを Claude に分析させる。

「ぐるぐるAIプロジェクト」の思想（依頼→AI処理→ログ記録→反応収集→AI分析・学習→次回に反映）
のうち「AI分析・学習」を担う。やること:

  1. data/impressions.db から対象月の posts × metrics（各ツイートの最新観測値）を集計
  2. Claude API に「伸びる投稿の型・要素」の抽出と「次月アクション案」の生成を依頼
     （tool use で構造化 JSON を強制し、パース失敗を防ぐ）
  3. 結果を data/reports/{YYYY-MM}.json（構造化データ）と .md（人が読む用）に保存

自動投稿は一切行わない。分析結果は人が見て採否を判断する前提。
GitHub Actions からは `python -m scripts.analyze_monthly` で呼ぶ想定
（対象月は既定で「実行時点のUTC月」＝月末実行時にはその月全体）。
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from . import config as cfg
from . import db

_ANALYSIS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "submit_monthly_analysis",
    "description": "GMOグローバルスタジオX公式アカウントの月次投稿分析結果を送信する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "top_patterns": {
                "type": "array",
                "description": "インプレッション・エンゲージメントが伸びた投稿に共通する型・要素。",
                "items": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "型・要素の名前（短い日本語）"},
                        "evidence": {"type": "string", "description": "根拠となる具体的な投稿の特徴・数値"},
                        "why_it_works": {"type": "string", "description": "なぜ伸びると考えられるか"},
                    },
                    "required": ["pattern", "evidence", "why_it_works"],
                },
            },
            "underperforming_patterns": {
                "type": "array",
                "description": "伸びなかった投稿に共通する型・要素。",
                "items": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "evidence": {"type": "string"},
                        "why_it_works": {"type": "string", "description": "なぜ伸びなかったと考えられるか"},
                    },
                    "required": ["pattern", "evidence", "why_it_works"],
                },
            },
            "next_month_actions": {
                "type": "array",
                "description": "来月に採るべき具体的なアクション案。人が採否を判断する前提のため、実行可能な粒度で。",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "具体的なアクション（例: 投稿の型・時間帯・構成など）"},
                        "rationale": {"type": "string", "description": "このアクションを提案する根拠"},
                    },
                    "required": ["action", "rationale"],
                },
            },
            "summary": {
                "type": "string",
                "description": "月全体の総括（日本語、3〜5文程度）。Slack通知の冒頭に使う。",
            },
        },
        "required": ["top_patterns", "underperforming_patterns", "next_month_actions", "summary"],
    },
}

_SYSTEM_PROMPT = """あなたはGMOグローバルスタジオの公式X（旧Twitter）アカウント運用を支援するSNS分析アシスタントです。
渡された1ヶ月分の投稿データ（本文・投稿時刻・画像有無・インプレッション・いいね・リポスト・返信・引用・リンククリック等）を分析し、
「伸びる投稿の型・要素」と「伸びなかった投稿の型・要素」を具体的な根拠とともに抽出し、次月に採るべき具体的なアクション案を提示してください。

制約:
- 自動投稿は行われません。あなたの分析結果は人間が見て採否を判断し、次の投稿に反映します。
- 抽象的な助言（「もっと魅力的な投稿を」等）ではなく、データから読み取れる具体的な型・要素を述べてください。
- 投稿件数が少ない、または偏りがある場合はその限界も率直に述べてください。
- すべて日本語で出力してください。
- 分析結果は submit_monthly_analysis ツールで送信してください。"""


def _month_bounds(year_month: str) -> tuple[str, str]:
    """'YYYY-MM' から [月初, 翌月月初) の ISO8601 文字列を返す。"""
    year, month = (int(part) for part in year_month.split("-"))
    start = dt.date(year, month, 1)
    days_in_month = calendar.monthrange(year, month)[1]
    end = start + dt.timedelta(days=days_in_month)
    return f"{start.isoformat()}T00:00:00+00:00", f"{end.isoformat()}T00:00:00+00:00"


def _default_month() -> str:
    """既定の対象月（実行時点のUTC月, 'YYYY-MM'）。"""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _row_to_feature(row) -> dict[str, Any]:
    posted_at = dt.datetime.fromisoformat(row["posted_at"])
    engagements = row["likes"] + row["reposts"] + row["replies"] + row["quotes"]
    impressions = row["impressions"]
    return {
        "tweet_id": row["tweet_id"],
        "text": row["text"],
        "char_count": len(row["text"]),
        "posted_at_utc": row["posted_at"],
        "posted_hour_utc": posted_at.hour,
        "posted_weekday": ["月", "火", "水", "木", "金", "土", "日"][posted_at.weekday()],
        "has_image": bool(row["has_image"]),
        "media_count": row["media_count"],
        "has_link": "http://" in row["text"] or "https://" in row["text"],
        "impressions": impressions,
        "likes": row["likes"],
        "reposts": row["reposts"],
        "replies": row["replies"],
        "quotes": row["quotes"],
        "link_clicks": row["link_clicks"],
        "engagement_rate": round(engagements / impressions, 4) if impressions > 0 else 0.0,
    }


def _aggregate_stats(features: list[dict[str, Any]]) -> dict[str, Any]:
    impressions = [f["impressions"] for f in features]
    return {
        "post_count": len(features),
        "impressions_total": sum(impressions),
        "impressions_mean": round(statistics.mean(impressions), 1) if impressions else 0,
        "impressions_median": statistics.median(impressions) if impressions else 0,
        "top_post": max(features, key=lambda f: f["impressions"]) if features else None,
        "bottom_post": min(features, key=lambda f: f["impressions"]) if features else None,
    }


def _call_claude(features: list[dict[str, Any]], stats: dict[str, Any], year_month: str) -> dict[str, Any]:
    import anthropic  # 遅延importにしてAPIキー未設定でも他関数がテスト可能なようにする

    api_key = cfg.get_anthropic_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません。")

    client = anthropic.Anthropic(api_key=api_key)
    user_content = (
        f"対象月: {year_month}\n"
        f"投稿件数: {stats['post_count']}\n"
        f"インプレッション合計: {stats['impressions_total']} / 平均: {stats['impressions_mean']} / 中央値: {stats['impressions_median']}\n\n"
        "以下は対象月の各投稿データ（JSON配列）です。\n"
        f"{json.dumps(features, ensure_ascii=False, indent=2)}"
    )

    response = client.messages.create(
        model=cfg.get_anthropic_model(),
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        tools=[_ANALYSIS_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_monthly_analysis"},
        messages=[{"role": "user", "content": user_content}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_monthly_analysis":
            return block.input

    raise RuntimeError("Claude からの分析結果（tool_use）が見つかりませんでした。")


def _render_markdown(year_month: str, stats: dict[str, Any], analysis: dict[str, Any]) -> str:
    lines = [
        f"# 月次分析レポート {year_month}",
        "",
        f"- 投稿件数: {stats['post_count']}",
        f"- インプレッション合計: {stats['impressions_total']}",
        f"- インプレッション平均: {stats['impressions_mean']} / 中央値: {stats['impressions_median']}",
        "",
        "## 総括",
        "",
        analysis["summary"],
        "",
        "## 伸びる投稿の型・要素",
        "",
    ]
    for item in analysis["top_patterns"]:
        lines += [
            f"### {item['pattern']}",
            f"- 根拠: {item['evidence']}",
            f"- 理由: {item['why_it_works']}",
            "",
        ]
    lines += ["## 伸びなかった投稿の型・要素", ""]
    for item in analysis["underperforming_patterns"]:
        lines += [
            f"### {item['pattern']}",
            f"- 根拠: {item['evidence']}",
            f"- 理由: {item['why_it_works']}",
            "",
        ]
    lines += ["## 次月アクション案（人が採否を判断してください）", ""]
    for i, item in enumerate(analysis["next_month_actions"], start=1):
        lines += [
            f"{i}. **{item['action']}**",
            f"   - 根拠: {item['rationale']}",
            "",
        ]
    return "\n".join(lines)


def run(year_month: str | None = None) -> dict[str, Any] | None:
    """月次分析を実行し、レポートを保存する。データが無ければ None を返す。"""
    if year_month is None:
        year_month = _default_month()

    db_path = cfg.get_db_path()
    start_iso, end_iso = _month_bounds(year_month)

    with db.connect(db_path) as conn:
        db.init_db(conn)
        rows = db.get_month_posts_with_latest_metrics(conn, start_iso, end_iso)

    if not rows:
        print(f"[analyze_monthly] {year_month} の投稿データがありません。分析をスキップします。")
        return None

    features = [_row_to_feature(row) for row in rows]
    stats = _aggregate_stats(features)
    print(f"[analyze_monthly] 対象月={year_month} 投稿件数={stats['post_count']}")

    analysis = _call_claude(features, stats, year_month)

    report = {
        "month": year_month,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stats": {k: v for k, v in stats.items() if k not in ("top_post", "bottom_post")},
        "top_post_tweet_id": stats["top_post"]["tweet_id"] if stats["top_post"] else None,
        "bottom_post_tweet_id": stats["bottom_post"]["tweet_id"] if stats["bottom_post"] else None,
        "analysis": analysis,
    }

    reports_dir = cfg.get_reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{year_month}.json"
    md_path = reports_dir / f"{year_month}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(year_month, stats, analysis), encoding="utf-8")

    print(f"[analyze_monthly] レポート保存: {json_path}")
    print(f"[analyze_monthly] レポート保存: {md_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="月次投稿データをClaudeに分析させる")
    parser.add_argument(
        "--month",
        dest="month",
        default=None,
        help="対象月 YYYY-MM（省略時は実行時点のUTC月）",
    )
    args = parser.parse_args()

    try:
        report = run(year_month=args.month)
    except Exception as exc:  # noqa: BLE001
        print(f"[analyze_monthly] エラー: {exc}", file=sys.stderr)
        sys.exit(1)

    if report is None:
        sys.exit(0)


if __name__ == "__main__":
    main()
