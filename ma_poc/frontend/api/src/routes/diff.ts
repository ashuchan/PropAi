import { Router } from 'express';
import type { IDiffService } from '../../../services/src/interfaces/IDiffService.js';
export function createDiffRoutes(diffService: IDiffService): Router {
  const router = Router();
  router.get('/latest', async (_req, res, next) => { try { res.json(await diffService.getLatestDiff()); } catch (err) { next(err); } });
  router.get('/:date', async (req, res, next) => { try { res.json(await diffService.getDailyDiff(req.params.date)); } catch (err) { next(err); } });
  return router;
}
