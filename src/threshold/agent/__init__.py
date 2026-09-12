"""The triage agent: everything a model is allowed to touch, and nothing else.

Its own package rather than a module under `services/`, because the interesting
property here is a boundary and a boundary is easier to check when it has a
perimeter. A test can reflect over one package and say what the agent can do.
"""
