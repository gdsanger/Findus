# No models of its own; this module's mere presence makes Django treat
# apps.ingest as having a `models_module`, which is what `post_migrate`
# requires to fire for this app (see apps.py / schedules.py, #1060).
