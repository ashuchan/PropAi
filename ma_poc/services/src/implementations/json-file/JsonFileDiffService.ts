/**
 * @file JsonFileDiffService.ts
 * @description Computes daily diffs by comparing consecutive run outputs.
 */

import type { IDiffService } from '../../interfaces/IDiffService.js';
import type { DailyDiff, DiffSummary, RentChange, PropertyChange, ConcessionChange, ChangelogEntry } from '../../types/diff.js';
import { readJsonFile, getRunDates, runPath } from './dataLoader.js';

interface RawProperty {
  'Unique ID': string;
  'Property ID': string;
  'Property Name': string;
  units: Array<{
    unit_id: string;
    market_rent_low: number;
    market_rent_high: number;
    concessions: string | null;
    available_date: string;
  }>;
}

export class JsonFileDiffService implements IDiffService {
  constructor(private readonly dataDir: string) {}

  async getDailyDiff(date: string): Promise<DailyDiff> {
    const dates = await getRunDates(this.dataDir);
    const dateIndex = dates.indexOf(date);
    const previousDate = dateIndex >= 0 && dateIndex < dates.length - 1 ? dates[dateIndex + 1] : null;

    const current = await readJsonFile<RawProperty[]>(runPath(this.dataDir, date, 'properties.json'));
    const previous = previousDate
      ? await readJsonFile<RawProperty[]>(runPath(this.dataDir, previousDate, 'properties.json'))
      : null;

    if (!current) return this.emptyDiff(date, previousDate || date);
    return this.computeDiff(date, previousDate || date, current, previous || []);
  }

  async getLatestDiff(): Promise<DailyDiff> {
    const dates = await getRunDates(this.dataDir);
    if (dates.length === 0) return this.emptyDiff('', '');
    return this.getDailyDiff(dates[0]);
  }

  async getPropertyChangelog(_propertyId: string, _days?: number): Promise<ChangelogEntry[]> {
    return [];
  }

  private computeDiff(date: string, previousDate: string, current: RawProperty[], previous: RawProperty[]): DailyDiff {
    const prevMap = new Map(previous.map(p => [p['Unique ID'] || p['Property ID'], p]));
    const currMap = new Map(current.map(p => [p['Unique ID'] || p['Property ID'], p]));

    const rentChanges: RentChange[] = [];
    const propertyChanges: PropertyChange[] = [];
    const concessionChanges: ConcessionChange[] = [];
    let rentsIncreased = 0, rentsDecreased = 0, newAvailable = 0;
    let becameLeased = 0, newConcessions = 0, disappearedUnits = 0;

    for (const [id, curr] of currMap) {
      const prev = prevMap.get(id);
      if (!prev) {
        propertyChanges.push({ propertyId: id, propertyName: curr['Property Name'], changeType: 'NEW', details: `New property with ${curr.units?.length || 0} units`, timestamp: date });
        continue;
      }

      const prevUnits = new Map((prev.units || []).map(u => [u.unit_id, u]));
      const currUnits = new Map((curr.units || []).map(u => [u.unit_id, u]));

      for (const [unitId, currUnit] of currUnits) {
        const prevUnit = prevUnits.get(unitId);
        if (!prevUnit) { newAvailable++; continue; }

        const prevRent = (prevUnit.market_rent_low + prevUnit.market_rent_high) / 2;
        const currRent = (currUnit.market_rent_low + currUnit.market_rent_high) / 2;
        if (Math.abs(currRent - prevRent) > 1) {
          const change = currRent - prevRent;
          const direction = change > 0 ? 'up' as const : 'down' as const;
          if (direction === 'up') rentsIncreased++; else rentsDecreased++;
          rentChanges.push({ propertyId: id, propertyName: curr['Property Name'], unitId, previousRent: Math.round(prevRent), currentRent: Math.round(currRent), change: Math.round(change), changePercent: prevRent > 0 ? Math.round((change / prevRent) * 100) : 0, direction });
        }

        if (prevUnit.concessions !== currUnit.concessions) {
          if (currUnit.concessions && !prevUnit.concessions) newConcessions++;
          concessionChanges.push({ propertyId: id, propertyName: curr['Property Name'], previousConcession: prevUnit.concessions, currentConcession: currUnit.concessions, changeType: !prevUnit.concessions && currUnit.concessions ? 'NEW' : prevUnit.concessions && !currUnit.concessions ? 'REMOVED' : 'MODIFIED' });
        }
      }

      for (const unitId of prevUnits.keys()) {
        if (!currUnits.has(unitId)) disappearedUnits++;
      }
    }

    for (const [id, prev] of prevMap) {
      if (!currMap.has(id)) {
        propertyChanges.push({ propertyId: id, propertyName: prev['Property Name'], changeType: 'DISAPPEARED', details: 'Property no longer in output', timestamp: date });
      }
    }

    const summary: DiffSummary = { rentsIncreased, rentsDecreased, newAvailable, becameLeased, newConcessions, disappearedUnits };
    return { date, previousDate, summary, rentChanges, propertyChanges, concessionChanges };
  }

  private emptyDiff(date: string, previousDate: string): DailyDiff {
    return { date, previousDate, summary: { rentsIncreased: 0, rentsDecreased: 0, newAvailable: 0, becameLeased: 0, newConcessions: 0, disappearedUnits: 0 }, rentChanges: [], propertyChanges: [], concessionChanges: [] };
  }
}
