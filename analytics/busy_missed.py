"""Why inbound calls were missed: was the agent already on another call?

    python -m analytics.busy_missed data/snapshot.json 2026-10-02 2026-10-07 exports/busy_missed

Input is a ``scripts/fetch_team_data.py`` snapshot. Each missed inbound call is
pinned to an agent (the user on the call record, else the lead's owner) and put
in exactly one bucket:

- ``busy``      – the agent had another call in progress at that moment
- ``near_call`` – not mid-call, but within ``NEAR_SECS`` of starting/ending one
                  (dialling, wrap-up, notes)
- ``free``      – the agent had no call nearby: the miss was not caused by a call
- ``off_hours`` – outside the IST working day (and the agent was not mid-call)
- ``no_agent``  – neither the call nor the lead names a team member

A call occupies the line from ``start_utc`` for ``duration`` seconds; a call with
no recorded duration (unanswered dial, ring-out) is given ``RING_SECS``.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from analytics.team_report import IST, WORK_END, WORK_START, utc

RING_SECS = 30   # line time assumed for a call with no duration (ringing / not answered)
NEAR_SECS = 120  # "just before / just after another call" window
BUCKETS = ("busy", "near_call", "free", "off_hours", "no_agent")


def pct(a, b):
    return round(100 * a / b, 1) if b else 0.0


def analyse(snap: dict, start: str, end: str) -> dict:
    users = {u["ID"]: f"{u.get('FirstName', '')} {u.get('LastName', '')}".strip() for u in snap["users"]}
    by_name = {n: uid for uid, n in users.items()}
    owner = {l["ProspectID"]: l.get("OwnerId") for l in snap["leads"]}
    d0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=IST)
    d1 = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)

    def agent_of(c):
        if c.get("user_id") in users:
            return c["user_id"]
        return by_name.get((c.get("caller") or "").strip())

    calls = []
    for c in snap["calls"]:
        t = utc(c.get("start_utc"))
        if not t:
            continue
        calls.append({**c, "t": t, "agent": agent_of(c),
                      "end": t + timedelta(seconds=c.get("duration") or RING_SECS)})

    # every interval during which an agent's line was in use (answered or not)
    line = defaultdict(list)
    for c in calls:
        if c["agent"] and not (c["direction"] == "inbound" and c["status"] != "Answered"):
            line[c["agent"]].append(c)
    by_lead = defaultdict(list)
    for c in calls:
        by_lead[c["lead_id"]].append(c)

    rows = []
    for c in calls:
        if c["direction"] != "inbound" or c["status"] == "Answered" or not d0 <= c["t"] < d1:
            continue
        agent = c["agent"] or (owner.get(c["lead_id"]) if owner.get(c["lead_id"]) in users else None)
        local = c["t"].astimezone(IST)
        overlap = near = None
        for o in line.get(agent, []):
            if o["t"] <= c["t"] < o["end"]:
                overlap = o
                break
            gap = min(abs((c["t"] - o["end"]).total_seconds()), abs((o["t"] - c["t"]).total_seconds()))
            if gap <= NEAR_SECS and (near is None or gap < near[1]):
                near = (o, gap)
        if not agent:
            bucket = "no_agent"
        elif overlap:
            bucket = "busy"
        elif not WORK_START <= local.hour < WORK_END:
            bucket = "off_hours"
        elif near:
            bucket = "near_call"
        else:
            bucket = "free"
        other = overlap or (near[0] if near else None)
        later = sorted((x for x in by_lead[c["lead_id"]] if x["t"] > c["t"]), key=lambda x: x["t"])
        cb = next((x for x in later if x["direction"] == "outbound"), None)
        rows.append({
            "lead_id": c["lead_id"],
            "agent": users.get(agent, ""),
            "agent_from": "call" if c["agent"] else ("lead_owner" if agent else ""),
            "missed_at_ist": local.strftime("%Y-%m-%d %H:%M:%S"),
            "hour_ist": local.hour,
            "status": c["status"],
            "bucket": bucket,
            "other_call_direction": other["direction"] if other else "",
            "other_call_status": other["status"] if other else "",
            "other_call_start_ist": other["t"].astimezone(IST).strftime("%H:%M:%S") if other else "",
            "other_call_secs": other.get("duration") or 0 if other else "",
            "mins_to_callback": round((cb["t"] - c["t"]).total_seconds() / 60) if cb else None,
            "reconnected": any(x["status"] == "Answered" for x in later),
        })
    rows.sort(key=lambda r: r["missed_at_ist"])

    total = len(rows)
    buckets = Counter(r["bucket"] for r in rows)
    summary = {
        "missed_inbound": total,
        "unique_leads": len({r["lead_id"] for r in rows}),
        **{f"{b}": buckets[b] for b in BUCKETS},
        **{f"{b}_%": pct(buckets[b], total) for b in BUCKETS},
        "busy_on_outbound": sum(1 for r in rows if r["bucket"] == "busy" and r["other_call_direction"] == "outbound"),
        "busy_on_inbound": sum(1 for r in rows if r["bucket"] == "busy" and r["other_call_direction"] == "inbound"),
        "busy_never_called_back": sum(1 for r in rows if r["bucket"] == "busy" and r["mins_to_callback"] is None),
        "busy_called_back_within_15m": sum(1 for r in rows if r["bucket"] == "busy"
                                           and r["mins_to_callback"] is not None and r["mins_to_callback"] <= 15),
    }

    def breakdown(key):
        g = defaultdict(list)
        for r in rows:
            g[r[key]].append(r)
        out = []
        for k, rs in sorted(g.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
            bc = Counter(r["bucket"] for r in rs)
            out.append({key: k, "missed": len(rs), **{b: bc[b] for b in BUCKETS},
                        "busy_%": pct(bc["busy"], len(rs))})
        return out

    by_hour = sorted(breakdown("hour_ist"), key=lambda r: r["hour_ist"])
    return {"summary": summary, "by_agent": breakdown("agent"), "by_hour": by_hour, "rows": rows}


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main(snapshot, start, end, out_dir):
    r = analyse(json.load(open(snapshot)), start, end)
    os.makedirs(out_dir, exist_ok=True)
    for key in ("by_agent", "by_hour", "rows"):
        write_csv(os.path.join(out_dir, f"{key}.csv"), r[key])
    json.dump(r, open(os.path.join(out_dir, "report.json"), "w"), indent=1, default=str)
    print(json.dumps({"summary": r["summary"], "by_agent": r["by_agent"]}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
