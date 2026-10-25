"""Serializable plan visibility policy for both SQL pagination and detail reads."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanScope:
    markets: tuple = ('*',)
    instruments: tuple = ('*',)

    @staticmethod
    def _restricted(values):
        return bool(values) and '*' not in values

    @staticmethod
    def _matches(value, values):
        return not value or not PlanScope._restricted(values) or value.strip().upper() in {
            item.strip().upper() for item in values
        }

    def allows(self, snapshot):
        instrument = snapshot.get('instrument') or {}
        if not self._matches(instrument.get('market'), self.markets):
            return False
        if not self._matches(instrument.get('symbol'), self.instruments):
            return False
        return all(self.allows({'instrument': ref['evidence_snapshot']})
                   for ref in snapshot.get('research_refs', []) if ref.get('evidence_snapshot'))

    def sql(self, expression='v.snapshot'):
        # The expression is supplied only by this module's repository, never by a caller.
        conditions, params = [], []
        for key, values in (('market', self.markets), ('symbol', self.instruments)):
            if not self._restricted(values):
                continue
            allowed = [value.strip().upper() for value in values]
            field = f"COALESCE({expression}->'instrument'->>'{key}', '')"
            evidence = f"COALESCE(ref->'evidence_snapshot'->>'{key}', '')"
            conditions.append(f"({field} = '' OR UPPER({field}) = ANY(%s::text[]))")
            params.append(allowed)
            conditions.append(f"NOT EXISTS (SELECT 1 FROM jsonb_array_elements(COALESCE({expression}->'research_refs', '[]'::jsonb)) ref WHERE {evidence} <> '' AND NOT (UPPER({evidence}) = ANY(%s::text[])))")
            params.append(allowed)
        return (' AND '.join(conditions) or 'TRUE'), params
