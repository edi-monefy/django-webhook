from celery import states

RETRYING = "RETRYING"
INVALID = "INVALID"

STATES = [
    (states.PENDING, states.PENDING),
    (RETRYING, RETRYING),
    (states.FAILURE, states.FAILURE),
    (states.SUCCESS, states.SUCCESS),
    (INVALID, INVALID),
]

RESENDABLE_STATES = [states.SUCCESS, states.FAILURE]
TERMINAL_STATES = [states.SUCCESS, states.FAILURE, INVALID]

TOPIC_REGEX = r"\w+\.\w+\/[create|update|delete]"
