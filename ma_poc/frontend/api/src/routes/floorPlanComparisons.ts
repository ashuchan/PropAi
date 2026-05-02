import { Router } from 'express';
import type { IFloorPlanComparisonService } from '../../../services/src/interfaces/IFloorPlanComparisonService.js';

export function createFloorPlanComparisonRoutes(
  svc: IFloorPlanComparisonService,
): Router {
  const router = Router();

  // Capability probe — frontend hides the section when unsupported.
  router.get('/_meta', (_req, res, next) => {
    void (async () => {
      try {
        res.json({
          supported: svc.isSupported(),
          latestRunId: await svc.getLatestRunId(),
        });
      } catch (err) {
        next(err);
      }
    })();
  });

  router.get('/', (req, res, next) => {
    void (async () => {
      try {
        const runId = typeof req.query.runId === 'string' ? req.query.runId : undefined;
        const overview = await svc.getOverview(runId);
        res.json(overview);
      } catch (err) {
        next(err);
      }
    })();
  });

  router.get('/:canonicalId', (req, res, next) => {
    void (async () => {
      try {
        const runId =
          typeof req.query.runId === 'string'
            ? req.query.runId
            : await svc.getLatestRunId();
        if (!runId) {
          res.status(404).json({ error: 'No comparison runs available' });
          return;
        }
        const deep = await svc.getPropertyDeepDive(runId, req.params.canonicalId);
        if (!deep) {
          res.status(404).json({ error: 'Property not found in comparison run' });
          return;
        }
        res.json(deep);
      } catch (err) {
        next(err);
      }
    })();
  });

  return router;
}
