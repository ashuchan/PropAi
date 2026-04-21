/**
 * @file DiffService.ts
 * @description Impl-agnostic daily-diff service. Compares current-run and
 * previous-run property lists via the provider.
 */

import type { IDataProvider, RawRunProperty } from '../data-provider/contracts.js';
import type { IDiffService } from '../interfaces/IDiffService.js';
import type {
  DailyDiff, DiffSummary, RentChange, PropertyChange, ConcessionChange, ChangelogEntry,
} from '../types/diff.js';

interface RawUnit {
  unit_id?: string;
  market_rent_low?: number;
  market_rent_high?: number;
  rent_low?: number;
  rent_high?: number;
  concessions?: string | null;
  available_date?: string;
}

function getPropId(p: RawRunProperty): string {
  return (
    (p['Unique ID'] as string) ||
    (p['Property ID'] as string) ||
    (p.apartment_id != null ? String(p.apartment_id) : '')
  );
}

function getPropName(p: RawRunProperty): string {
  return (p['Property Name'] as string) || (p.proj_name as string) || getPropId(p);
}

function getRent(u: RawUnit): [number, number] {
  return [u.rent_low ?? u.market_rent_low ?? 0, u.rent_high ?? u.market_rent_high ?? 0];
}

export class DiffService implements IDiffService {
  constructor(private readonly provider: IDataProvider) {}

  async getDailyDiff(date: string): Promise<DailyDiff> {
    const dates = await this.provider.runs.listDates();
    const idx = dates.indexOf(date);
    const previousDate = idx >= 0 && idx < dates.length - 1 ? dates[idx + 1] : null;

    const current = await this.provider.properties.listForRun(date);
    const previous = previousDate ? await this.provider.properties.listForRun(previousDate) : [];
    if (current.length === 0) return this.emptyDiff(date, previousDate || date);
    return this.computeDiff(date, previousDate || date, current, previous);
  }

  async getLatestDiff(): Promise<DailyDiff> {
    const dates = await this.provider.runs.listDates();
    if (dates.length === 0) return this.emptyDiff('', '');
    return this.getDailyDiff(dates[0]);
  }

  async getPropertyChangelog(_propertyId: string, _days?: number): Promise<ChangelogEntry[]> {
    return [];
  }

  private computeDiff(
    date: string,
    previousDate: string,
    current: RawRunProperty[],
    previous: RawRunProperty[],
  ): DailyDiff {
    const prevMap = new Map(previous.map((p) => [getPropId(p), p]));
    const currMap = new Map(current.map((p) => [getPropId(p), p]));

    const rentChanges: RentChange[] = [];
    const propertyChanges: PropertyChange[] = [];
    const concessionChanges: ConcessionChange[] = [];
    let rentsIncreased = 0, rentsDecreased = 0, newAvailable = 0;
    let becameLeased = 0, newConcessions = 0, disappearedUnits = 0;

    for (const [id, curr] of currMap) {
      if (!id) continue;
      const prev = prevMap.get(id);
      const currUnits = (curr.units as RawUnit[] | undefined) ?? [];
      if (!prev) {
        propertyChanges.push({
          propertyId: id,
          propertyName: getPropName(curr),
          changeType: 'NEW',
          details: `New property with ${currUnits.length} units`,
          timestamp: date,
        });
        continue;
      }

      const prevUnits = new Map(((prev.units as RawUnit[] | undefined) ?? []).map((u) => [u.unit_id || '', u]));
      const currUnitMap = new Map(currUnits.map((u) => [u.unit_id || '', u]));

      for (const [unitId, currUnit] of currUnitMap) {
        if (!unitId) continue;
        const prevUnit = prevUnits.get(unitId);
        if (!prevUnit) { newAvailable++; continue; }

        const [prevLo, prevHi] = getRent(prevUnit);
        const [currLo, currHi] = getRent(currUnit);
        const prevRent = (prevLo + prevHi) / 2;
        const currRent = (currLo + currHi) / 2;
        if (Math.abs(currRent - prevRent) > 1) {
          const change = currRent - prevRent;
          const direction = change > 0 ? ('up' as const) : ('down' as const);
          if (direction === 'up') rentsIncreased++; else rentsDecreased++;
          rentChanges.push({
            propertyId: id,
            propertyName: getPropName(curr),
            unitId,
            previousRent: Math.round(prevRent),
            currentRent: Math.round(currRent),
            change: Math.round(change),
            changePercent: prevRent > 0 ? Math.round((change / prevRent) * 100) : 0,
            direction,
          });
        }

        if (prevUnit.concessions !== currUnit.concessions) {
          if (currUnit.concessions && !prevUnit.concessions) newConcessions++;
          concessionChanges.push({
            propertyId: id,
            propertyName: getPropName(curr),
            previousConcession: prevUnit.concessions ?? null,
            currentConcession: currUnit.concessions ?? null,
            changeType:
              !prevUnit.concessions && currUnit.concessions
                ? 'NEW'
                : prevUnit.concessions && !currUnit.concessions
                  ? 'REMOVED'
                  : 'MODIFIED',
          });
        }
      }

      for (const unitId of prevUnits.keys()) {
        if (unitId && !currUnitMap.has(unitId)) disappearedUnits++;
      }
    }

    for (const [id, prev] of prevMap) {
      if (!id || currMap.has(id)) continue;
      propertyChanges.push({
        propertyId: id,
        propertyName: getPropName(prev),
        changeType: 'DISAPPEARED',
        details: 'Property no longer in output',
        timestamp: date,
      });
    }

    const summary: DiffSummary = {
      rentsIncreased, rentsDecreased, newAvailable, becameLeased, newConcessions, disappearedUnits,
    };
    return { date, previousDate, summary, rentChanges, propertyChanges, concessionChanges };
  }

  private emptyDiff(date: string, previousDate: string): DailyDiff {
    return {
      date,
      previousDate,
      summary: {
        rentsIncreased: 0, rentsDecreased: 0, newAvailable: 0,
        becameLeased: 0, newConcessions: 0, disappearedUnits: 0,
      },
      rentChanges: [],
      propertyChanges: [],
      concessionChanges: [],
    };
  }
}
