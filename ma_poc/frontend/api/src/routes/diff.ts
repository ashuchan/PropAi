import { Router } from 'express';
import type { IDiffService } from '../../../services/src/interfaces/IDiffService.js';
import { isPotentiallyPurged, sendPurged } from '../middleware/retention.js';
export function createDiffRoutes(diffService: IDiffService): Router {
  const router = Router();
  router.get('/latest', async (_req, res, next) => { try { res.json(await diffService.getLatestDiff()); } catch (err) { next(err); } });
  router.get('/:date', async (req, res, next) => {
    try {
      // Diff is computed against `property_snapshots` for both the
      // requested date and its predecessor. Both rows are subject to
      // the 3-day retention sweep, so any date older than the window
      // can't produce a meaningful diff regardless of what the
      // underlying service returns. Short-circuit with a clear 410.
      if (isPotentiallyPurged(req.params.date)) {
        sendPurged(res, req.params.date, 'property_snapshots');
        return;
      }
      res.json(await diffService.getDailyDiff(req.params.date));
    } catch (err) { next(err); }
  });
  return router;
}
