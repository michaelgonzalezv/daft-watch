"""Sleep until this slot's time (t0 + slot * 8h). A job can run at most 6 h, so if the
target is further away it sleeps as long as it safely can and says "relay": the workflow
then re-dispatches itself with the same inputs instead of capturing early.

Prints  wait_result=capture | relay  for the workflow to read.
"""
import datetime as dt
import sys
import time

slot, t0 = int(sys.argv[1]), dt.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
SPACING_H, MAX_SLEEP_S = 8, 5 * 3600 + 40 * 60           # stay well under the 6 h job limit
target = t0 + dt.timedelta(hours=SPACING_H * slot)
now = dt.datetime.now(dt.timezone.utc)
remaining = (target - now).total_seconds()
print(f"slot {slot} target {target.isoformat()} now {now.isoformat()} remaining {remaining:.0f}s", flush=True)
if remaining <= 0:
    print("wait_result=capture")
elif remaining <= MAX_SLEEP_S:
    time.sleep(remaining)
    print("wait_result=capture")
else:
    time.sleep(MAX_SLEEP_S)
    print("wait_result=relay")
