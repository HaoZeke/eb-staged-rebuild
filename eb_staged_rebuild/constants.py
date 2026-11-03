"""Named policy values and their sources."""


# ns-051 carries a 120-hour site walltime plus one day.  The notes explicitly
# say the walltime was not measured for the deployment site; operators must
# override this value after answering open-004.
NOTES_MAX_WALLTIME_HOURS = 120
RETIREMENT_SAFETY_MARGIN_HOURS = 24
DEFAULT_GRACE_HOURS = NOTES_MAX_WALLTIME_HOURS + RETIREMENT_SAFETY_MARGIN_HOURS
SECONDS_PER_HOUR = 60 * 60

# Run identifiers use UTC and are also human-sortable in hidden retired names.
RUN_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
