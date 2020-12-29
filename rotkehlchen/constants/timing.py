ETH_DAO_FORK_TS = 1469020840  # 2016-07-20 13:20:40 UTC
BTC_BCH_FORK_TS = 1501593374  # 2017-08-01 13:16:14 UTC

ROTKEHLCHEN_SERVER_TIMEOUT = 5
GLOBAL_REQUESTS_TIMEOUT = 5  # perhaps consolidate this and the one above?

YEAR_IN_SECONDS = 31536000  # 60 * 60 * 24 * 365

# For queries that are attempted multiple times:
# How many times to retry an external query before giving up
QUERY_RETRY_TIMES = 5

# Seconds for which cached api queries will be cached
# By default 10 minutes.
# TODO: Make configurable!
CACHE_RESPONSE_FOR_SECS = 600
