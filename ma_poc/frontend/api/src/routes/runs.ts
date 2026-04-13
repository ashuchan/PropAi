import { Router } from 'express';
import type { IRunService } from '../../../services/src/interfaces/IRunService.js';
import { validateQuery, limitSchema } from '../middleware/validation.js';
export function createRunRoutes(runService: IRunService): Router {
  const router = Router();
  router.get('/', validateQuery(limitSchema), async (req, res, next) => { try { res.json(await runService.getRunHistory(parseInt(req.query.limit as string) || 30)); } catch (err) { next(err); } });
  router.get('/latest', async (_req, res, next) => { try { res.json(await runService.getLatestRun()); } catch (err) { next(err); } });
  router.get('/:date', async (req, res, next) => { try { const r = await runService.getRunByDate(req.params.date); if (!r) { res.status(404).json({ error: 'Run not found' }); return; } res.json(r); } catch (err) { next(err); } });
  return router;
}
