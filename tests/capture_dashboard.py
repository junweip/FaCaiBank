"""Save the CloudWatch dashboard as PNGs for a test window, plus alarm history and RDS events.

Every alarm that went into ALARM during the window is also rendered as an alarm graph
(metric, threshold line and the red ALARM band).

Renders every metric widget of the facaibank-<env> dashboard with CloudWatch's
GetMetricWidgetImage for exactly the window of a live test, so the report can show
what the dashboard looked like during that run without console screenshots.

    python tests/capture_dashboard.py --label load-test --start 2026-11-08T10:00:00Z --end 2026-11-08T10:40:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).parent))
from live_support import EVIDENCE_DIR, write_evidence  # noqa: E402


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "widget"


def widget_images(dashboard_body: dict, start: datetime, end: datetime, width: int = 900, height: int = 400):
    """Yield (title, MetricWidget JSON) for each metric widget, pinned to the window."""
    for index, widget in enumerate(dashboard_body.get("widgets", [])):
        if widget.get("type") != "metric":
            continue
        properties = dict(widget["properties"])
        properties.update({"start": start.isoformat(), "end": end.isoformat(), "width": width, "height": height})
        properties.pop("liveData", None)
        title = properties.get("title", f"widget {index}")
        properties["title"] = f"{title} ({start:%H:%M}-{end:%H:%M} UTC)"
        yield title, properties


def alarm_widget(alarm: dict, fired_at: list[str], start: datetime, end: datetime) -> dict:
    """The alarm's metric with its threshold (red above) and a marker where it went to ALARM."""
    dimensions = [value for d in alarm.get("Dimensions", []) for value in (d["Name"], d["Value"])]
    return {
        "title": f"Alarm {alarm['AlarmName']} ({start:%H:%M}-{end:%H:%M} UTC)",
        "metrics": [[alarm["Namespace"], alarm["MetricName"], *dimensions]],
        "stat": alarm.get("Statistic", "Maximum"),
        "period": alarm.get("Period", 60),
        "view": "timeSeries",
        "annotations": {
            "horizontal": [
                {"value": alarm["Threshold"], "label": f"ALARM threshold ({alarm['ComparisonOperator']})", "fill": "above", "color": "#d13212"}
            ],
            "vertical": [{"value": at, "label": "ALARM", "color": "#d13212"} for at in fired_at],
        },
        "start": start.isoformat(),
        "end": end.isoformat(),
        "width": 900,
        "height": 400,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="Test name, used for the output folder")
    parser.add_argument("--start", required=True, type=parse_time)
    parser.add_argument("--end", required=True, type=parse_time)
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-1"))
    parser.add_argument("--env", default=os.getenv("FACAI_ENV", "dev"))
    args = parser.parse_args(argv)

    cloudwatch = boto3.client("cloudwatch", region_name=args.region)
    body = json.loads(cloudwatch.get_dashboard(DashboardName=f"facaibank-{args.env}")["DashboardBody"])
    folder = EVIDENCE_DIR / "figures" / f"{args.label}-{args.start:%Y%m%dT%H%M%SZ}"
    folder.mkdir(parents=True, exist_ok=True)
    for index, (title, widget) in enumerate(widget_images(body, args.start, args.end), start=1):
        image = cloudwatch.get_metric_widget_image(MetricWidget=json.dumps(widget), OutputFormat="png")["MetricWidgetImage"]
        path = folder / f"{index:02d}-{slug(title)}.png"
        path.write_bytes(image)
        print(f"saved {path}")

    alarms = []
    for page in cloudwatch.get_paginator("describe_alarm_history").paginate(
        StartDate=args.start, EndDate=args.end, HistoryItemType="StateUpdate"
    ):
        alarms += [
            {"at": item["Timestamp"].isoformat(), "alarm": item["AlarmName"], "summary": item["HistorySummary"]}
            for item in page["AlarmHistoryItems"]
            if item["AlarmName"].startswith(f"facaibank-{args.env}")
        ]
    fired = sorted({item["alarm"] for item in alarms if "to ALARM" in item["summary"]})
    for name in fired:
        definition = cloudwatch.describe_alarms(AlarmNames=[name])["MetricAlarms"][0]
        times = [item["at"] for item in alarms if item["alarm"] == name and "to ALARM" in item["summary"]]
        widget = alarm_widget(definition, times, args.start, args.end)
        image = cloudwatch.get_metric_widget_image(MetricWidget=json.dumps(widget), OutputFormat="png")["MetricWidgetImage"]
        path = folder / f"alarm-{slug(name)}.png"
        path.write_bytes(image)
        print(f"saved {path}")
    rds = boto3.client("rds", region_name=args.region)
    events = [
        {"at": event["Date"].isoformat(), "source": event["SourceIdentifier"], "message": event["Message"]}
        for page in rds.get_paginator("describe_events").paginate(
            SourceType="db-instance", StartTime=args.start, EndTime=args.end
        )
        for event in page["Events"]
    ]
    path = write_evidence(
        "history",
        {
            "label": args.label,
            "window": {"start": args.start.isoformat(), "end": args.end.isoformat()},
            "alarm_state_changes": sorted(alarms, key=lambda item: item["at"]),
            "rds_events": sorted(events, key=lambda item: item["at"]),
        },
        folder / "history.json",
    )
    print(f"saved {path} ({len(alarms)} alarm changes, {len(events)} RDS events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
