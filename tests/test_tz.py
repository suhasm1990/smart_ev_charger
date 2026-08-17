import time
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

TZ = ZoneInfo("America/Los_Angeles")

class FormatterTZ(logging.Formatter):
    def converter(self, timestamp):
        dt = datetime.fromtimestamp(timestamp, tz=TZ)
        return dt.timetuple()

def test_tz():
    f = FormatterTZ('%(asctime)s | %(message)s')
    r = logging.LogRecord("name", logging.INFO, "path", 1, "msg", (), None)
    r.created = time.time()
    print("Formatter output:", f.format(r))
    print("datetime.now output:", datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))

    import schedule
    schedule.clear()
    schedule.every().day.at("23:50", TZ.key).do(lambda: None)
    print("Schedule with tz:", schedule.jobs[0].next_run)
    assert len(schedule.jobs) == 1
    print("✅ test_tz passed!")

if __name__ == "__main__":
    test_tz()
