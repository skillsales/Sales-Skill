from analytics.busy_missed import analyse


def call(lead, user, t, direction="outbound", status="Answered", dur=200, caller="A B"):
    return {"lead_id": lead, "user_id": user, "start_utc": t, "direction": direction,
            "status": status, "duration": dur, "caller": caller}


SNAP = {
    "users": [{"ID": "u1", "FirstName": "A", "LastName": "B"}, {"ID": "u2", "FirstName": "C", "LastName": "D"}],
    "leads": [{"ProspectID": "L2", "OwnerId": "u1"}, {"ProspectID": "L9", "OwnerId": "someone-else"}],
    "calls": [
        # u1 talks to L1 05:30:00–05:35:00 UTC (11:00–11:05 IST)
        call("L1", "u1", "2026-10-06 05:30:00", dur=300),
        # L2 rings u1 mid-conversation -> busy (agent taken from lead owner); called back 10 min later
        call("L2", None, "2026-10-06 05:32:00", direction="inbound", status="Missed", dur=0, caller=""),
        call("L2", "u1", "2026-10-06 05:42:00"),
        # L3 rings u1 90 s after that call ended -> near_call
        call("L3", "u1", "2026-10-06 05:36:30", direction="inbound", status="Missed", dur=0),
        # L4 rings u2 with nothing nearby -> free
        call("L4", "u2", "2026-10-06 08:00:00", direction="inbound", status="Missed", dur=0, caller="C D"),
        # L5 rings u2 at 22:00 IST -> off_hours
        call("L5", "u2", "2026-10-06 16:30:00", direction="inbound", status="Missed", dur=0, caller="C D"),
        # u2 dials out unanswered (no duration -> 30 s ring) and L6 rings 10 s in -> busy
        call("L6x", "u2", "2026-10-06 09:00:00", status="NotAnswered", dur=0, caller="C D"),
        call("L6", None, "2026-10-06 09:00:10", direction="inbound", status="Missed", dur=0, caller="C D"),
        # owner is not on the team -> no_agent
        call("L9", None, "2026-10-06 06:00:00", direction="inbound", status="Missed", dur=0, caller=""),
        # outside the date range -> ignored
        call("L7", "u1", "2026-10-08 05:00:00", direction="inbound", status="Missed", dur=0),
    ],
}


def test_buckets():
    r = analyse(SNAP, "2026-10-06", "2026-10-06")
    b = {x["lead_id"]: x for x in r["rows"]}
    assert {k: v["bucket"] for k, v in b.items()} == {
        "L2": "busy", "L3": "near_call", "L4": "free", "L5": "off_hours", "L6": "busy", "L9": "no_agent"}
    assert b["L2"]["agent"] == "A B" and b["L2"]["agent_from"] == "lead_owner"
    assert b["L2"]["mins_to_callback"] == 10 and b["L2"]["reconnected"]
    assert b["L6"]["agent_from"] == "call" and b["L6"]["other_call_status"] == "NotAnswered"

    s = r["summary"]
    assert s["missed_inbound"] == 6 and s["busy"] == 2 and s["busy_%"] == 33.3
    assert s["busy_on_outbound"] == 2 and s["busy_never_called_back"] == 1
    agents = {x["agent"]: x for x in r["by_agent"]}
    assert agents["C D"]["missed"] == 3 and agents["C D"]["busy"] == 1
