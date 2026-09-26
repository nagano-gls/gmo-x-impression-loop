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


def _build_slack_payload(report: dict[str, Any]) -> dict[str, Any]:
    month = report["month"]
    stats = report["stats"]
    analysis = report["analysis"]

    lines = [
        f"*📊 X投稿 月次分析レポート — {month}*",
        "",
        analysis["summary"],
        "",
        f"投稿件数: {stats['post_count']} / インプレッション合計: {stats['impressions_total']}"
        f" / 平均: {stats['impressions_mean']} / 中央値: {stats['impressions_median']}",
        "",
        "*✅ 伸びる投稿の型・要素*",
    ]
    for item in analysis["top_patterns"]:
        lines.append(f"• *{item['pattern']}* — {item['why_it_works']}")

    lines += ["", "*⚠️ 伸びなかった投稿の型・要素*"]
    for item in analysis["underperforming_patterns"]:
        lines.append(f"• *{item['pattern']}* — {item['why_it_works']}")

    lines += ["", "*🎯 次月アクション案（採否はご判断ください）*"]
    for i, item in enumerate(analysis["next_month_actions"], start=1):
        lines.append(f"{i}. *{item['action']}* — {item['rationale']}")

    lines += ["", "_この投稿は自動生成された分析です。自動投稿は行われません。採否は運用担当者の判断でお願いします。_"]

    text = "\n".join(lines)
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}},
        ],
    }


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
