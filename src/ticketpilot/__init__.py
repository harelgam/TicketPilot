"""TicketPilot — AI-assisted support ticket triage with deterministic validation.

The design principle throughout: the model *proposes*, the application
*decides*. Every closed vocabulary, the ticket id, and the recommended-action
text are owned and enforced by code, so a model failure or an injected
instruction degrades the result into a reviewable abstention rather than an
authoritative-looking fabrication.
"""

__version__ = "0.1.0"
