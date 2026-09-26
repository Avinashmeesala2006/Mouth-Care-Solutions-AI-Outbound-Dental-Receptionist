from dataclasses import dataclass

@dataclass
class UsageMeter:
    envelope_minutes: int = 3000
    used_minutes: float = 0
    def record(self, minutes: float) -> dict:
        self.used_minutes += max(0, minutes)
        percent = self.used_minutes / self.envelope_minutes * 100
        threshold = '100%' if percent >= 100 else '95%' if percent >= 95 else '85%' if percent >= 85 else '70%' if percent >= 70 else 'below_70%'
        return {'minutes': self.used_minutes, 'percent_used': round(percent, 2), 'threshold': threshold, 'outbound_allowed': percent < 95, 'billable_calling_allowed': percent < 100}
''
