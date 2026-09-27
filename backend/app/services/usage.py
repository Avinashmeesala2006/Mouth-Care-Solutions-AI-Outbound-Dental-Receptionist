"""Service-quota alert thresholds (the quota itself is tracked per call in PostgreSQL)."""


def threshold_label(percent_used: float) -> str:
    if percent_used >= 100:
        return '100%'
    if percent_used >= 95:
        return '95%'
    if percent_used >= 85:
        return '85%'
    if percent_used >= 70:
        return '70%'
    return 'below_70%'
