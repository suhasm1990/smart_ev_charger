import time
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

TZ = ZoneInfo("America/Los_Angeles")

class FormatterTZ(logging.Formatter):
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=TZ)
        return dt.timetuple()

f = FormatterTZ('%(asctime)s | %(message)s')
r = logging.LogRecord("name", logging.INFO, "path", 1, "msg", (), None)
# manually set created to current time
r.created = time.time()
print("Formatter output:", f.format(r))
print("datetime.now output:", datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))

import schedule
schedule.every().day.at("23:50").do(lambda: None)
print("Schedule without tz:", schedule.jobs[0].next_run)

schedule.clear()
schedule.every().day.at("23:50", TZ.key).do(lambda: None)
print("Schedule with tz:", schedule.jobs[0].next_run)
