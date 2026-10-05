"""Compare each Final TX file against its corresponding _updated file
to quantify what the stats-tab overlay added or replaced."""
import json, os

datasets = [
    ('TX boys 24-25',  'Final_scraped_data/Final_tx_accumulated_boys_24_25.json',
                       'Texas_scraped_data/tx_accumulated_stats_boys_2024_2025_updated.json'),
    ('TX boys 25-26',  'Final_scraped_data/Final_tx_accumulated_boys_25_26.json',
                       'Arkansas_scraped_data/tx_accumulated_stats_boys_2025_2026_updated.json'),
    ('TX girls 24-25', 'Final_scraped_data/Final_tx_accumulated_girls_24_25.json',
                       'Texas_scraped_data/tx_accumulated_stats_girls_2024_2025_updated.json'),
    ('TX girls 25-26', 'Final_scraped_data/Final_tx_accumulated_girls_25_26.json',
                       'Texas_scraped_data/tx_accumulated_stats_girls_2025_2026_updated.json'),
]


def load(fp):
    with open(fp, encoding='utf-8') as f:
        return json.load(f)


def player_key(r):
    return (r['team_id'], r['team_name'], r['Name'])


def team_key(r):
    return (r['team_id'], r['team_name'])


grand = {
    'records_before': 0, 'records_after': 0, 'records_delta': 0,
    'players_only_final': 0, 'players_only_updated': 0,
    'team_totals_changed_gp': 0, 'team_totals_changed_tgc': 0,
    'new_team_totals_with_data': 0,
}

for label, final_fp, upd_fp in datasets:
    print()
    print('=' * 78)
    print(f'  {label}')
    print(f'  Final  : {final_fp}')
    print(f'  Updated: {upd_fp}')
    print('=' * 78)
    final = load(final_fp)
    upd = load(upd_fp)

    print(f'  Records — updated: {len(upd):>6d}   final: {len(final):>6d}   '
          f'delta: {len(final) - len(upd):+d}')

    # team_total comparison
    upd_tt   = {team_key(r): r for r in upd   if r['record_type'] == 'team_total'}
    final_tt = {team_key(r): r for r in final if r['record_type'] == 'team_total'}
    print(f'  team_totals — updated: {len(upd_tt):>5d}   final: {len(final_tt):>5d}')

    # Player rows
    upd_pl   = {player_key(r): r for r in upd   if r['record_type'] == 'player'}
    final_pl = {player_key(r): r for r in final if r['record_type'] == 'player'}
    only_in_final   = set(final_pl) - set(upd_pl)
    only_in_updated = set(upd_pl) - set(final_pl)
    print(f'  player rows — updated: {len(upd_pl):>5d}   final: {len(final_pl):>5d}')
    print(f'    new players in Final (not in Updated)       : {len(only_in_final):>5d}')
    print(f'    removed players (in Updated, gone in Final) : {len(only_in_updated):>5d}')

    # Team_totals where GP or TGC differs (i.e. stats-tab overrode per-game)
    gp_changed = 0
    tgc_changed = 0
    new_data_teams = 0   # was GP=0 in updated, now GP>0 in final
    for k, fr in final_tt.items():
        ur = upd_tt.get(k)
        if not ur:
            if (fr.get('GP') or 0) > 0:
                new_data_teams += 1
            continue
        u_gp  = ur.get('GP') or 0
        f_gp  = fr.get('GP') or 0
        u_tgc = ur.get('TotalGamesChecked')
        f_tgc = fr.get('TotalGamesChecked')
        if u_gp != f_gp:
            gp_changed += 1
        if u_tgc != f_tgc:
            tgc_changed += 1
        if u_gp == 0 and f_gp > 0:
            new_data_teams += 1
    print(f'  team_totals where GP changed    : {gp_changed:>4d}')
    print(f'  team_totals where TGC changed   : {tgc_changed:>4d}')
    print(f'  team_totals NEW data (was GP=0) : {new_data_teams:>4d}')

    grand['records_before'] += len(upd)
    grand['records_after']  += len(final)
    grand['records_delta']  += len(final) - len(upd)
    grand['players_only_final']   += len(only_in_final)
    grand['players_only_updated'] += len(only_in_updated)
    grand['team_totals_changed_gp']  += gp_changed
    grand['team_totals_changed_tgc'] += tgc_changed
    grand['new_team_totals_with_data'] += new_data_teams

print()
print('=' * 78)
print('  GRAND TOTAL — TX (4 datasets)')
print('=' * 78)
print(f'  Records BEFORE (in _updated files): {grand["records_before"]:>6d}')
print(f'  Records AFTER  (in Final files):    {grand["records_after"]:>6d}')
print(f'  Net delta:                          {grand["records_delta"]:>+6d}')
print()
print(f'  Player rows ADDED by stats-tab    : {grand["players_only_final"]:>6d}')
print(f'  Player rows REMOVED by stats-tab  : {grand["players_only_updated"]:>6d}')
print(f'    (per-game players replaced by stats-tab players for the same team)')
print()
print(f'  team_totals where GP value changed  : {grand["team_totals_changed_gp"]:>5d}')
print(f'  team_totals where TGC value changed : {grand["team_totals_changed_tgc"]:>5d}')
print(f'  Teams that went from GP=0 → GP>0    : {grand["new_team_totals_with_data"]:>5d}')
print('=' * 78)
