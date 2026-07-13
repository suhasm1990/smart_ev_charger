import logging
import sys
import os

from config import TEXT_LOG_FILE

# Ensure logs directory exists
os.makedirs(os.path.dirname(TEXT_LOG_FILE) or ".", exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s"

logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(TEXT_LOG_FILE),
    ],
)
log = logging.getLogger("EV_CHARGER")

log_netzero     = logging.getLogger("NETZERO")
log_chargepoint = logging.getLogger("CHARGEPOINT")
log_decision    = logging.getLogger("DECISION")
log_mode        = logging.getLogger("MODE")
log_csv         = logging.getLogger("CSV")

# Silence verbose urllib3 connection logs
logging.getLogger("urllib3").setLevel(logging.WARNING)
