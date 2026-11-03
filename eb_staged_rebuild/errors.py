class StagedRebuildError(Exception):
    """A failed run that left the live software directory alone."""


class Refused(StagedRebuildError):
    """The operation was refused before it could empty or hide a live path."""


class NeedsIntervention(StagedRebuildError):
    """The journal and the live module file disagree. Nothing further was changed."""
