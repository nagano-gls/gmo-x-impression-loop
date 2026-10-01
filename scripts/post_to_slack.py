"""月次ジョブ: analyze_monthly.py が生成したレポートを Slack に通知する。

「ぐるぐるAIプロジェクト」の思想における「反応収集」の一環として、
分析結果を人（運用担当者）に届けることだけを行う。

自動投稿は一切行わない。Slack Incoming Webhook で指定チャンネルに
サマリを投稿するだけで、X への投稿や返信は行わない。
GitHub Actions からは `python -m scripts.post_to_slack` で呼ぶ想定
（既定で data/reports 内の最新レポートを送信）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests

from . import config as cfg


def _find_latest_report(reports_dir: Path) -> Path | None:
    """reports_dir 内の {YYYY-MM}.json を月順で最新のものを返す。"""
    candidates = sorted(reports_dir.glob("????-??.json"))
    return candidates[-1] if candidates else None


def _load_report(reports_dir: Path, year_month: str | None) -> tuple[str, dict[str, Any]]:
    if year_month is not None:
        path = reports_dir / f"{year_month}.json"
        if not path.exists():
            raise FileNotFoundError(f"レポートが見つかりません: {path}")
    else:
        path = _find_latest_report(reports_dir)
        if path is None:
            raise FileNotFoundError(f"レポートが1件もありません: {reports_dir}")
    report = json.loads(path.read_text(encoding="utf-8"))
    return path.stem, report


_BULLET_LINE_LIMIT = 220  # 1行が長文化してスキャン性を落とさないよう要約側で切る


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bullet_block(title: str, items: list[dict[str, str]]) -> dict[str, Any]:
    """「見出し＋箇条書き」の section block を1つ作る。根拠(evidence)は省き型と理由だけに絞る。"""
    lines = [f"*{title}*"]
    for item in items:
        line = f"• *{item['pattern']}* — {item['why_it_works']}"
        lines.append(_truncate(line, _BULLET_LINE_LIMIT))
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _build_slack_payload(report: dict[str, Any]) -> dict[str, Any]:
    month = report["month"]
    stats = report["stats"]
    analysis = report["analysis"]

    action_lines = ["*🎯 次月アクション案（採否はご判断ください）*"]
    for i, item in enumerate(analysis["next_month_actions"], start=1):
        line = f"{i}. *{item['action']}* — {item['rationale']}"
        action_lines.append(_truncate(line, _BULLET_LINE_LIMIT))

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 X投稿 月次分析レポート — {month}", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": analysis["summary"]}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*投稿件数*\n{stats['post_count']}"},
                {"type": "mrkdwn", "text": f"*インプレ合計*\n{stats['impressions_total']:,}"},
                {"type": "mrkdwn", "text": f"*平均*\n{stats['impressions_mean']:,.0f}"},
                {"type": "mrkdwn", "text": f"*中央値*\n{stats['impressions_median']:,}"},
            ],
        },
        {"type": "divider"},
        _bullet_block("✅ 伸びる投稿の型・要素", analysis["top_patterns"]),
        {"type": "divider"},
        _bullet_block("⚠️ 伸びなかった投稿の型・要素", analysis["underperforming_patterns"]),
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(action_lines)}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "🤖 自動生成された分析です。自動投稿は行われません。採否は運用担当者の判断でお願いします。",
                }
            ],
        },
    ]

    # 通知プレビュー等で使われるフォールバックテキスト（blocks未対応クライアント向け）
    fallback_text = f"X投稿 月次分析レポート — {month}: {_truncate(analysis['summary'], 150)}"

    return {"text": fallback_text, "blocks": blocks}


def run(year_month: str | None = None) -> bool:
    """レポートをSlackに投稿する。成功したら True。"""
    webhook_url = cfg.get_slack_webhook_url()
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL が設定されていません。")

    reports_dir = cfg.get_reports_dir()
    label, report = _load_report(reports_dir, year_month)
    payload = _build_slack_payload(report)

    response = requests.post(webhook_url, json=payload, timeout=30)
    response.raise_for_status()

    print(f"[post_to_slack] レポート({label})をSlackに投稿しました。")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="月次分析レポートをSlackに投稿する")
    parser.add_argument(
        "--month",
        dest="month",
        default=None,
        help="対象月 YYYY-MM（省略時は data/reports 内の最新レポート）",
    )
    args = parser.parse_args()

    try:
        run(year_month=args.month)
    except Exception as exc:  # noqa: BLE001
        print(f"[post_to_slack] エラー: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
