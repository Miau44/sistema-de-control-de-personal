"""Read-only independent October grouping comparison; no operational identity confirmation."""
from collections import defaultdict,Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from attendance_grouping import group_events
from reconciliation import parse_zlink_xlsx


def main():
    from core import DB_PATH
    path=DB_PATH
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row
        raw=[dict(r) for r in c.execute("SELECT * FROM biometric_events WHERE occurred_at>='2026-10-01' AND occurred_at<'2026-11-01' ORDER BY occurred_at,id")]
        source=parse_zlink_xlsx((Path.home()/'Downloads'/'Biometrico(20261001-20261031).xlsx').read_bytes())
        assert Counter(r['row_fingerprint'] for r in raw)==Counter(r['fingerprint'] for r in source['rows'])
        daily=defaultdict(list);physical=Counter()
        for e in raw:
            e['provenance']='B_ZLINK_MANUAL' if 'manual' in e['event_type'].lower() or 'administrador' in e['source'].lower() else 'A_ZLINK_PHYSICAL'
            e['original_event_id']=e['id'];daily[(e['person_id'],e['occurred_at'][:10])].append(e)
            if e['provenance']=='A_ZLINK_PHYSICAL':physical[(e['person_id'],e['occurred_at'][:10],e['occurred_at'][11:16])]+=1
        groups=[g for (person,day),events in daily.items() for g in group_events(events,day,{'id':1,'window_seconds':60})]
        hr=Counter();admin=manual=0;compared=0
        for r in c.execute('''SELECT p.identity_code,d.* FROM hr_reference_days d JOIN hr_roster_proposals p ON p.id=d.proposal_id'''):
            compared+=1
            for e in json.loads(r['physical_json']):hr[(r['identity_code'],r['work_date'],e['time'])]+=1
            admin+=len(json.loads(r['administrative_json']));manual+=len(json.loads(r['manual_json']))
        assert hr==physical
        preserved_extrema=0
        for key,events in daily.items():
            gs=[g for g in groups if g['person_id']==key[0] and g['first_at'][:10]==key[1]]
            assert min(e['occurred_at'] for e in events)==min(g['first_at'] for g in gs)
            assert max(e['occurred_at'] for e in events)==max(g['last_at'] for g in gs)
            preserved_extrema+=1
        result={'scope':'Exploratorio por ID original y fecha civil; no confirma vínculos ni horarios',
            'window_seconds':60,'rule':'Desde primer evento, inclusivo, sin encadenar; solo físicos mismo ID/SN; manuales separados',
            'original_events':len(raw),'source_excel_fingerprints_identical':True,
            'physical_events':sum(physical.values()),'manual_events':sum(e['provenance']=='B_ZLINK_MANUAL' for e in raw),
            'effective_groups':len(groups),'repeated_groups':sum(g['original_count']>1 for g in groups),
            'extra_events':sum(g['original_count']-1 for g in groups),
            'hr_cells_compared':compared,'hr_physical_events_match_at_minute_precision':hr==physical,
            'hr_manual_lines':manual,'hr_administrative_lines_kept_separate':admin,
            'person_day_extrema_preserved':preserved_extrema,
            'jheyson_events':sum(e['person_id']=='8282765' for e in raw),
            'jheyson_groups':sum(g['person_id']=='8282765' for g in groups),
            'classifications_inferred':0,'work_hours_calculated':0}
        out=Path('instance/stage2_continuous');out.mkdir(exist_ok=True)
        (out/'grouping_october.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
