import { Router } from 'express';
import type { IRunService } from '../../../services/src/interfaces/IRunService.js';
import { validateQuery, limitSchema } from '../middleware/validation.js';

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function createRunRoutes(runService: IRunService): Router {
  const router = Router();
  router.get('/', validateQuery(limitSchema), async (req, res, next) => { try { res.json(await runService.getRunHistory(parseInt(req.query.limit as string) || 30)); } catch (err) { next(err); } });
  router.get('/latest', async (_req, res, next) => { try { res.json(await runService.getLatestRun()); } catch (err) { next(err); } });

  // LLM drill-down routes must register BEFORE the catch-all /:date so Express
  // does not match "/:date/llm" against the /:date handler.
  router.get('/:date/llm', async (req, res, next) => {
    try {
      const { date } = req.params;
      if (!DATE_RE.test(date)) { res.status(400).json({ error: 'Invalid date format (expected YYYY-MM-DD)' }); return; }
      const report = await runService.getLlmReport(date);
      if (!report) { res.status(404).json({ error: 'LLM report not found for this run' }); return; }
      res.json(report);
    } catch (err) { next(err); }
  });

  router.get('/:date/llm/:propertyId', async (req, res, next) => {
    try {
      const { date, propertyId } = req.params;
      if (!DATE_RE.test(date)) { res.status(400).json({ error: 'Invalid date format (expected YYYY-MM-DD)' }); return; }
      const detail = await runService.getLlmPropertyDetail(date, propertyId);
      if (!detail) { res.status(404).json({ error: 'Per-property LLM report not found' }); return; }
      res.json(detail);
    } catch (err) { next(err); }
  });

  router.get('/:date', async (req, res, next) => { try { const r = await runService.getRunByDate(req.params.date); if (!r) { res.status(404).json({ error: 'Run not found' }); return; } res.json(r); } catch (err) { next(err); } });
  return router;
}
